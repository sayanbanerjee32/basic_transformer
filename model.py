from re import X, sub
from networkx import directed_configuration_model
from sympy import div
import torch
import torch.nn as nn
import math

# this function required because the n LayerNormalisation does not have bias =0
class LayerNormliztion(nn.Module):
    def __init__(self,eps:float=10**-6) -> None:
        super().__init__()
        self.eps = eps
        self.alpha = nn.Parameter(torch.ones(1))
        self.bias = nn.Parameter(torch.ones(1))

    def forward(self, x):
        mean = x.mean(dim =-1, keepdim = True)
        std = x.std(dim=-1, keepdim= True)
        #eps is added to take care of the number when the std is 0
        return self.alpha * (x - mean)/(std + self.eps) + self.bias
    

class FeedForwardBlock(nn.Module):
    def __init__(self, d_model: int, d_ff:int, dropout: float):
        super().__init__()
        self.linear_1 = nn.Linear(d_model,d_ff) #squeeze and expand
        self.dropout = nn.Dropout(dropout)
        self.linear_2 = nn.Linear(d_model,d_ff)

    def forward(self,x):
        x = torch.relu(self.linear_1(x))
        x = self.dropout(x)
        x = self.linear_2(x)
        return x
    
class InputEmbeddings(nn.Module):
    def __init__(self, d_model: int, vocab_size: int):
        super().__init__()
        self.d_model = d_model
        self.vocab_size = vocab_size
        self.embedding = nn.Embedding(vocab_size, d_model) # random initialisation

    def forward(self, x):
        return self.embedding(x) * math.sqrt(self.d_model)
    
class PositionalEmbedding(nn.Module):
    def __init__(self, d_model: int, seq_len: int, dropout: float):
        super().__init__()
        self.d_model = d_model
        self.seq_len = seq_len
        self.dropout = nn.Dropout(dropout)
        pe = torch.zeros(seq_len,d_model)
        position = torch.arange(0, seq_len, dtype= torch.float).unsqueeze(1)
        # This creates [0, 1, 2, ..., seq_len] and unsqueeze will [[0], [1], [2], ...,[seq_len]]
        div_term = torch.exp(torch.arange(0, d_model, 2),float() * (-math.log(10000.0) / d_model))
        # log and exp is used for numerical stability

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term[:d_model//2])
        pe = pe.unsqueeze(0) #(1, seq_len, d_model)

        self.register_buffer('pe', pe)
        #pe is stored and not back propagated

    def forward(self, x):
        x = x + (self.pe[:, :x.shape[1], :]).requires_grad_(False) #(batch, seq_len, d_model)
        return self.dropout(x)
    
class ResidualConnection(nn.Module):
    def __init__(self, dropout: float):
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.norm = LayerNormliztion()

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))
    
class MultiHeadAttentionBlock(nn.Modul):
    def __init__(self, d_model: int, h: int, dropout: float):
        super().__init__()
        self.d_model = d_model
        self.h = h

        assert d_model % h == 0, "d_model not divisible by head"

        self.d_k = d_model // h
        self.w_q = nn.Linear(d_model, d_model, bias = False)
        self.w_k = nn.Linear(d_model, d_model, bias = False)
        self.w_v = nn.Linear(d_model, d_model, bias = False)
        self.w_o = nn.Linear(d_model, d_model, bias = False)
        self.dropout = nn.Dropout(dropout)

    @staticmethod
    def attention(query, key, value, mask, dropout: nn.Dropout):
        # mask can be encode mask or decoder mask
        d_k = query.shape[-1]
        attention_scores = (query @ key.transpose(-2, -1)) / math.sqrt(d_k)
        if mask is not None:
            attention_scores.masked_fill_(mask == 0, -1e9)
            #where every mask is 0 it is filled with small number
        attention_scores = attention_scores.softmax(dim = -1)
        # (batch, seq_len, seq_len)
        if dropout is not None:
            attention_scores = dropout(attention_scores)
        # (batch, h, seq_len) --> (batch, h, seq_len, d_k)
        return (attention_scores @ value), attention_scores
    
    def forward(self, q, k, v, mask):
        query = self.w_q(q) # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        key = self.w_k(k) # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        value = self.w_v(v) # (batch, seq_len, d_model) --> (batch, seq_len, d_model)
        
        x, self.attention_scores = MultiHeadAttentionBlock.attention(query, key, value, mask, self.dropout)

        # combine heades together
        # (batch, h, seq_len, d_k) -> (batch, h, seq_len, d_k) ->
        # -> (batch, remaining which is seq_len, h * d_k which is d_model)
        x = x.transpose(1, 2).contiguous().view(x.shape[0], -1, self.h*self.d_k)
        # Memory is provided continuously for the data
        return self.w_o(x)
    
class EncoderBlock(nn.Module):
    def __init__(self,
                 self_attention_block: MultiHeadAttentionBlock,
                 feed_forward_block: FeedForwardBlock,
                 dropout):
        super().__init__()
        self.self_attention_block = self_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connection = nn.ModuleList([ResidualConnection(dropout) for _ in range(2)])

    def forward(self, x, src_mask):
        x = self.residual_connection[0](x, lambda x : self.self_attention_block(x, x, x, src_mask))
        x = self.residual_connection[1](x, self.feed_forward_block)
        return x
    
class Encoder(nn.Module):
    def __init__(self, layers: nn.ModuleList) -> None:
        super().__init__()
        self.layers = layers
        self.norm = LayerNormliztion()

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)
    
class DecoderBlock(nn.Module):
    def __init__(self,
                 self_attention_block: MultiHeadAttentionBlock,
                 cross_attention_block: MultiHeadAttentionBlock,
                 feed_forward_block: FeedForwardBlock,
                 dropout: float):
        super().__init__()
        self.self_attention_block = self_attention_block
        self.cross_attention_block = cross_attention_block
        self.feed_forward_block = feed_forward_block
        self.residual_connection = nn.ModuleList([ResidualConnection(dropout) for _ in range(3)])

    def forward(self, x, encoder_output, src_mask, tgt_mask):
        x = self.residual_connection[0](x, lambda x : self.self_attention_block(x, x, x, tgt_mask))
        x = self.residual_connection[1](x, lambda x : self.cross_attention_block(x, encoder_output, encoder_output, src_mask))
        x = self.residual_connection[2](x, self.feed_forward_block)
        return x
    
class Decoder(nn.Module):
    def __init__(self, layers: nn.ModuleList):
        super().__init__()
        self.layers = layers
        self.norm = LayerNormliztion()
    def forward(self, x, encoder_output, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, encoder_output, src_mask, tgt_mask)
        return self.norm(x)
    
class ProjectionLayer(nn.Module):
    def __init__(self, d_model, vocab_size) -> None:
        super().__init__()
        self.proj = nn.Linear(d_model, vocab_size)
    def forward(self, x):
        # (batch, seq_len, d_model) --> (batch, seq_len, vocab_size)
        return torch.log_softmax(self.proj(x), dim = -1)
    
class Transformer(nn.Module):
    def __init__(self,
                 encoder: Encoder,
                 decoder: Decoder,
                 src_embed: InputEmbeddings,
                 tgt_embed: InputEmbeddings,
                 src_pos: PositionalEmbedding,
                 tgt_pos: PositionalEmbedding,
                 projection_layer: ProjectionLayer
                 ):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.src_pos = src_pos
        self.tgt_pos = tgt_pos
        self.projection_layer = projection_layer

    def encode(self, src, src_mask):
        # (batch, seq_len, d_model)
        src = self.src_embed(src)
        src = self.src_pos(src)
        return self.encoder(src, src_mask)
    
    def decode(self,
               encoder_output: torch.Tensor,
               src_mask: torch.Tensor,
               tgt: torch.Tensor,
               tgt_mask: torch.Tensor):
        #(batch, seq_len, d_model)
        tgt = self.tgt_embed(tgt)
        tgt = self.tgt_pos(tgt)
        return self.decoder(tgt, encoder_output, src_mask, tgt_mask)
    
    def project(self, x):
        return self.projection_layer(x)
    
    def build_transformer(src_vocab_size: int,
                          tgt_vocab_size: int,
                          src_seq_len: int,
                          tgt_seq_len: int,
                          d_model: int = 512,
                          N: int = 6,
                          h: int = 8,
                          dropout: float = 0.1,
                          d_ff: int = 2048):
        # Create Embedding Layers
        src_embed = InputEmbeddings(d_model, src_vocab_size)
        tgt_embed = InputEmbeddings(d_model, tgt_vocab_size)

        # Create Positional encoding layers
        src_pos = PositionalEmbedding(d_model, src_seq_len, dropout)
        tgt_pos = PositionalEmbedding(d_model, tgt_seq_len, dropout)

        # Create encoder block
        encoder_blocks = []
        for _ in range(N):
            encoder_self_attention_block = MultiHeadAttentionBlock(d_model, h, dropout)
            feed_forward_block = FeedForwardBlock(d_model, d_ff, dropout)
            encoder_block = EncoderBlock(encoder_self_attention_block, feed_forward_block, dropout)
            encoder_blocks.append(encoder_block)

        decoder_blocks = []
        for _ in range(N):
            decoder_self_attention_block = MultiHeadAttentionBlock(d_model, h, dropout)
            decoder_cross_attention_block = MultiHeadAttentionBlock(d_model, h, dropout)
            feed_forward_block = FeedForwardBlock(d_model, d_ff, dropout)
            decoder_block = DecoderBlock(decoder_self_attention_block, 
                                         decoder_cross_attention_block,
                                         feed_forward_block,
                                         dropout)
            decoder_blocks.append(decoder_block)

        encoder = Encoder(nn.ModuleList(encoder_blocks))
        decoder = Decoder(nn.ModuleList(decoder_blocks))

        projection_layer = ProjectionLayer(d_model, tgt_vocab_size)
        transformer = Transformer(encoder, decoder, src_embed, tgt_embed, src_pos, tgt_pos, projection_layer)

        for p in transformer.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform(p)

        return transformer



