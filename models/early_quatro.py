
import torch
from torch import nn

from einops import rearrange, repeat
from einops.layers.torch import Rearrange
import timm
import random

# helpers

def pair(t):
    return t if isinstance(t, tuple) else (t, t)

# classes

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)

class Attention(nn.Module):
    def __init__(self, dim, heads = 8, dim_head = 64, dropout = 0.):
        super().__init__()
        inner_dim = dim_head *  heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim = -1)
        self.dropout = nn.Dropout(dropout)

        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        attn = self.attend(dots)
        attn = self.dropout(attn)

        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim, dropout = 0.):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads = heads, dim_head = dim_head, dropout = dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout = dropout))
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x

class EarlyMultimodal(nn.Module):
    def __init__(self, *, model_name, image_size, patch_size, num_classes, dim, depth, heads, mlp_dim, pool='cls', channels=3, dim_head=64, dropout=0., emb_dropout=0., p_visual_dropout=0., p_feature_dropout=0.):
        super().__init__()

        self.feature_extractor_1 = timm.create_model(
            model_name=model_name,
            pretrained=True,
            in_chans=channels,
            img_size=image_size,
        )

        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert image_height % patch_height == 0 and image_width % patch_width == 0, 'Image dimensions must be divisible by the patch size.'
        assert pool in {'cls', 'mean'}, 'pool type must be either cls (cls token) or mean (mean pooling)'

        self.to_lab_embedding = nn.Sequential(
            nn.Linear(48, dim),
        )

        num_patches = (image_height // patch_height) * (image_width // patch_width) + 1 
        num_patches += 76 # Account for lab value

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)
        self.p_visual_dropout = p_visual_dropout
        self.p_feature_dropout = p_feature_dropout

        self.transformer = Transformer(dim, depth, heads, dim_head, mlp_dim, dropout)

        self.pool = pool
        self.to_latent = nn.Identity()

        self.mlp_head = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_classes)
        )

        # Initialize BERT tokenizer and model
        self.tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
        self.bert_model = AutoModel.from_pretrained("bert-base-uncased")

    def forward(self, data):
        img_1, ehr, discharge_note, radiology_note = data[0], data[1], data[2], data[3]
        
        # Visual dropout
        if self.training:
            if random.random() <= self.p_visual_dropout:
                img_1 = torch.zeros_like(img_1)

        # Feature dropout
        if self.training:
            if random.random() <= self.p_feature_dropout:
                ehr = torch.zeros_like(ehr)

        x_image1 = self.feature_extractor_1.forward_features(img_1)

        ehr = ehr.permute(0, 2, 1)
        x_ehr = self.to_lab_embedding(ehr)

        # Process discharge notes
        discharge_note_tokens = self.tokenizer(discharge_note, padding=True, truncation=True, return_tensors="pt")
        discharge_note_embedding = self.bert_model(**discharge_note_tokens).last_hidden_state.mean(dim=1)

        # Process radiology notes
        radiology_note_tokens = self.tokenizer(radiology_note, padding=True, truncation=True, return_tensors="pt")
        radiology_note_embedding = self.bert_model(**radiology_note_tokens).last_hidden_state.mean(dim=1)

        # Concatenate all features
        x = torch.cat((x_image1, x_ehr, discharge_note_embedding, radiology_note_embedding), dim=1)

        b, n, _ = x.shape

        cls_tokens = repeat(self.cls_token, '1 1 d -> b 1 d', b=b)
        x = torch.cat((cls_tokens, x), dim=1)
        x += self.pos_embedding[:, :(n + 1)]
        x = self.dropout(x)

        x = self.transformer(x)

        x = x.mean(dim=1) if self.pool == 'mean' else x[:, 0]

        x = self.to_latent(x)
        return self.mlp_head(x)
