import math
import time
import torch
import torch.nn as nn
from functools import partial
from timm.models.registry import register_model
from timm.models.layers import trunc_normal_ as __call_trunc_normal_
import torch.nn.functional as F
from timm.models.layers import drop_path, to_2tuple, trunc_normal_

def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': (0.5, 0.5, 0.5), 'std': (0.5, 0.5, 0.5),
        **kwargs
    }


class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)
    
    def extra_repr(self) -> str:
        return 'p={}'.format(self.drop_prob)


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        # x = self.drop(x)
        # commit this for the orignal BERT implement 
        x = self.fc2(x)
        x = self.drop(x)
        return x


class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])
        self.patch_shape = (img_size[0] // patch_size[0], img_size[1] // patch_size[1])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x, **kwargs):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x

# ------------------------------------------------------------------------------------------
def trunc_normal_(tensor, mean=0., std=1.):
    __call_trunc_normal_(tensor, mean=mean, std=std, a=-std, b=std)

class Attention(nn.Module):
    def __init__(
            self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0.,
            proj_drop=0., window_size=None, attn_head_dim=None):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        if attn_head_dim is not None:
            head_dim = attn_head_dim
        all_head_dim = head_dim * self.num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, all_head_dim * 3, bias=False)
        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.v_bias = nn.Parameter(torch.zeros(all_head_dim))
        else:
            self.q_bias = None
            self.v_bias = None
        
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(all_head_dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, bool_masked_pos=None):

        B, N, C = x.shape
        qkv_bias = None
        if self.q_bias is not None:
            qkv_bias = torch.cat((self.q_bias, torch.zeros_like(self.v_bias, requires_grad=False), self.v_bias))

        qkv = F.linear(input=x, weight=self.qkv.weight, bias=qkv_bias)
        qkv = qkv.reshape(B, N, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))    # (B, N_head, N, N)
        
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, -1) 
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

'''
Modified from Attention()
'''
class CrossAttention(nn.Module):
    def __init__(
            self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0.,
            proj_drop=0., window_size=None, attn_head_dim=None):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        if attn_head_dim is not None:
            head_dim = attn_head_dim
        all_head_dim = head_dim * self.num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, all_head_dim, bias=False)
        self.k = nn.Linear(dim, all_head_dim, bias=False)
        self.v = nn.Linear(dim, all_head_dim, bias=False)

        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.v_bias = nn.Parameter(torch.zeros(all_head_dim))
        else:
            self.q_bias = None
            self.k_bias = None
            self.v_bias = None

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(all_head_dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, bool_masked_pos=None, k=None, v=None):
        B, N, C = x.shape
        N_k = k.shape[1]
        N_v = v.shape[1]

        q_bias, k_bias, v_bias = None, None, None
        if self.q_bias is not None:
            q_bias = self.q_bias
            k_bias = torch.zeros_like(self.v_bias, requires_grad=False)
            v_bias = self.v_bias

        q = F.linear(input=x, weight=self.q.weight, bias=q_bias)
        q = q.reshape(B, N, 1, self.num_heads, -1).permute(2, 0, 3, 1, 4).squeeze(0)    # (B, N_head, N_q, dim)

        k = F.linear(input=k, weight=self.k.weight, bias=k_bias)
        k = k.reshape(B, N_k, 1, self.num_heads, -1).permute(2, 0, 3, 1, 4).squeeze(0)

        v = F.linear(input=v, weight=self.v.weight, bias=v_bias)   
        v = v.reshape(B, N_v, 1, self.num_heads, -1).permute(2, 0, 3, 1, 4).squeeze(0)

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))      # (B, N_head, N_q, N_k)
        
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, -1) 
        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., init_values=None, act_layer=nn.GELU, norm_layer=nn.LayerNorm,
                 window_size=None, attn_head_dim=None):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, window_size=window_size, attn_head_dim=attn_head_dim)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        if init_values > 0:
            self.gamma_1 = nn.Parameter(init_values * torch.ones((dim)),requires_grad=True)
            self.gamma_2 = nn.Parameter(init_values * torch.ones((dim)),requires_grad=True)
        else:
            self.gamma_1, self.gamma_2 = None, None

    def forward(self, x, bool_masked_pos=None):
        if self.gamma_1 is None:
            x = x + self.drop_path(self.attn(self.norm1(x), bool_masked_pos))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        else:
            x = x + self.drop_path(self.gamma_1 * self.attn(self.norm1(x), bool_masked_pos))
            x = x + self.drop_path(self.gamma_2 * self.mlp(self.norm2(x)))

        return x


class RegressorBlock(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., init_values=None, act_layer=nn.GELU, norm_layer=nn.LayerNorm,
                 window_size=None, attn_head_dim=None):
        super().__init__()
        self.norm1_q = norm_layer(dim)
        self.norm1_k = norm_layer(dim)
        self.norm1_v = norm_layer(dim)
        self.norm2_cross = norm_layer(dim)
        self.cross_attn =  CrossAttention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, window_size=window_size, attn_head_dim=attn_head_dim)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        mlp_hidden_dim = int(dim * mlp_ratio)
        
        self.mlp_cross = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        if init_values > 0:
            self.gamma_1_cross = nn.Parameter(init_values * torch.ones((dim)),requires_grad=True)
            self.gamma_2_cross = nn.Parameter(init_values * torch.ones((dim)),requires_grad=True)
        else:
            self.gamma_1_cross = nn.Parameter(torch.ones((dim)),requires_grad=False)
            self.gamma_2_cross = nn.Parameter(torch.ones((dim)),requires_grad=False)

    def forward(self, x_q, x_kv, pos_q, pos_k, bool_masked_pos):
        x = x_q + self.drop_path(self.gamma_1_cross * self.cross_attn(self.norm1_q(x_q + pos_q),
         bool_masked_pos, k=self.norm1_k(x_kv + pos_k), v=self.norm1_v(x_kv)))
        x = self.norm2_cross(x)
        x = x + self.drop_path(self.gamma_2_cross * self.mlp_cross(x))

        return x


'''
Encoder that extracts representations
'''
class VisionTransformerEncoder(nn.Module):
    def __init__(self, img_size=224, patch_size=16, in_chans=3, vocab_size=8192, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=None, init_values=None, attn_head_dim=None,
                 use_abs_pos_emb=True, init_std=0.02, args=None, **kwargs):
        super().__init__()
        self.num_features = self.embed_dim = embed_dim

        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches
        self.num_patches = num_patches

        # generate class token and pos embed
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = self.build_2d_sincos_position_embedding(embed_dim, use_cls_token=True)
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values, window_size=None,
                attn_head_dim=attn_head_dim,
            )
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)

        self.init_std = init_std

        # init the model
        trunc_normal_(self.cls_token, std=self.init_std)
        self.apply(self._init_weights)
        # rescale init function from beit
        # if it is not activated, it will be overwritten
        self.fix_init_weight()

    def build_2d_sincos_position_embedding(self, embed_dim=768, temperature=10000., use_cls_token=False):
        h, w = self.patch_embed.patch_shape
        grid_w = torch.arange(w, dtype=torch.float32)
        grid_h = torch.arange(h, dtype=torch.float32)
        grid_w, grid_h = torch.meshgrid(grid_w, grid_h)
        assert embed_dim % 4 == 0, 'Embed dimension must be divisible by 4 for 2D sin-cos position embedding'
        pos_dim = embed_dim // 4
        omega = torch.arange(pos_dim, dtype=torch.float32) / pos_dim
        omega = 1. / (temperature ** omega)
        out_w = torch.einsum('m,d->md', [grid_w.flatten(), omega])
        out_h = torch.einsum('m,d->md', [grid_h.flatten(), omega])
        pos_emb = torch.cat([torch.sin(out_w), torch.cos(out_w), torch.sin(out_h), torch.cos(out_h)], dim=1)[None, :, :]

        if not use_cls_token:
            pos_embed = nn.Parameter(pos_emb)
        else:
            pe_token = torch.zeros([1, 1, embed_dim], dtype=torch.float32)
            pos_embed = nn.Parameter(torch.cat([pe_token, pos_emb], dim=1))
        pos_embed.requires_grad = False
        return pos_embed

    def fix_init_weight(self):
        def rescale(param, layer_id):
            param.div_(math.sqrt(2.0 * layer_id))

        for layer_id, layer in enumerate(self.blocks):
            rescale(layer.attn.proj.weight.data, layer_id + 1)
            rescale(layer.mlp.fc2.weight.data, layer_id + 1)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=self.init_std)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=self.init_std)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_num_layers(self):
        return len(self.blocks)

    def forward_features(self, x, bool_masked_pos):
        x = self.patch_embed(x, bool_masked_pos=bool_masked_pos)
        batch_size, seq_len, dim = x.size()

        cls_tokens = self.cls_token.expand(batch_size, -1, -1)

        # unmasked embeddings
        x_unmasked = x[~bool_masked_pos].reshape(batch_size, -1, dim)
        x_unmasked = torch.cat((cls_tokens, x_unmasked), dim=1)

        if self.pos_embed is not None:
            pos_embed = self.pos_embed.expand(batch_size, self.num_patches+1, dim)
            pos_embed_unmasked = pos_embed[:,1:][~bool_masked_pos].reshape(batch_size, -1, dim) 
            pos_embed_unmasked = torch.cat((pos_embed[:,:1], pos_embed_unmasked),dim=1)
            x_unmasked = x_unmasked + pos_embed_unmasked

        x_unmasked = self.pos_drop(x_unmasked)

        for blk in self.blocks:
            x_unmasked = blk(x_unmasked, bool_masked_pos)

        x_unmasked = self.norm(x_unmasked)

        return x_unmasked

    def forward(self, x, bool_masked_pos, return_all_tokens=False):
        x = self.forward_features(x, bool_masked_pos=bool_masked_pos)
        return x

'''
Latent context regressor + decoder that solves the pretext task.
'''
class VisionTransformerNeck(nn.Module):
    def __init__(self, patch_size=16, num_classes=8192, embed_dim=768, depth=6, 
                 num_heads=12, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=None, init_values=None, num_patches=196, init_std=0.02, args=None, patch_shape=(14,14)):
        super().__init__()

        self.num_features = self.embed_dim = embed_dim
        self.patch_size = patch_size
        self.args = args

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        # context regressor
        self.regressor_blocks = nn.ModuleList([
            RegressorBlock(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values)
            for i in range(depth)])

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, args.decoder_depth)]
        self.decoder_blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values)
            for i in range(args.decoder_depth)])

        self.norm = norm_layer(embed_dim)
        self.norm2 = norm_layer(embed_dim)
        self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()
        
        self.init_std = init_std

        # init the model
        trunc_normal_(self.head.weight, std=self.init_std)
        self.apply(self._init_weights)
        self.fix_init_weight()

    def fix_init_weight(self):
        def rescale(param, layer_id):
            param.div_(math.sqrt(2.0 * layer_id))

        for layer_id, layer in enumerate(self.regressor_blocks):
            rescale(layer.cross_attn.proj.weight.data, layer_id + 1)
            rescale(layer.mlp_cross.fc2.weight.data, layer_id + 1)

        for layer_id, layer in enumerate(self.decoder_blocks):
            rescale(layer.attn.proj.weight.data, layer_id + 1)
            rescale(layer.mlp.fc2.weight.data, layer_id + 1)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=self.init_std)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=self.init_std)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}
        
    def forward(self, x_masked, x_unmasked, pos_embed_masked, pos_embed_unmasked, bool_masked_pos):                
        # latent contextual regressor
        for blk in self.regressor_blocks:
            x_masked = blk(x_masked, torch.cat([x_unmasked, x_masked], dim=1), pos_embed_masked, torch.cat([pos_embed_unmasked, pos_embed_masked], dim=1), bool_masked_pos)
        x_masked = self.norm(x_masked)
        latent_pred = x_masked
        
        x_masked = x_masked + pos_embed_masked  # add pos embed, like encoder
        for blk in self.decoder_blocks:
            x_masked = blk(x_masked)
        x_masked = self.norm2(x_masked)

        logits = self.head(x_masked)
        
        return logits, latent_pred

#----------base--------

class VisionTransformerForMaskedImageModeling(nn.Module):
    def __init__(self, img_size=224, patch_size=16, in_chans=3, vocab_size=8192, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=None, init_values=None, attn_head_dim=None,
                 use_abs_pos_emb=True, init_std=0.02, args=None, **kwargs):
        super().__init__()

        self.encoder = VisionTransformerEncoder(img_size=img_size, patch_size=patch_size, in_chans=in_chans, 
                 vocab_size=vocab_size, embed_dim=embed_dim, depth=depth,
                 num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                 drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=drop_path_rate,
                 norm_layer=norm_layer, init_values=init_values, attn_head_dim=attn_head_dim,
                 use_abs_pos_emb=use_abs_pos_emb, init_std=init_std, args=args)

        # alignment constraint
        self.teacher = VisionTransformerEncoder(img_size=img_size, patch_size=patch_size, in_chans=in_chans, 
                vocab_size=vocab_size, embed_dim=embed_dim, depth=depth,
                num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=drop_path_rate,
                norm_layer=norm_layer, init_values=init_values, attn_head_dim=attn_head_dim,
                use_abs_pos_emb=use_abs_pos_emb, init_std=init_std, args=args)

        self.init_std = init_std
        self.args = args
        self.num_patches = self.encoder.patch_embed.num_patches

        self.pretext_neck = VisionTransformerNeck(patch_size=patch_size, num_classes=args.decoder_num_classes, embed_dim=args.decoder_embed_dim, depth=args.regressor_depth,
            num_heads=args.decoder_num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop_rate=drop_rate, attn_drop_rate=attn_drop_rate,
            drop_path_rate=drop_path_rate, norm_layer=norm_layer, init_values=args.decoder_layer_scale_init_value, num_patches=self.num_patches, init_std=init_std, args=args)

        # encoder to decoder projection, borrowed from mae.
        if args.decoder_embed_dim != embed_dim:
            self.encoder_to_decoder = nn.Linear(embed_dim, args.decoder_embed_dim, bias=True)
            self.encoder_to_decoder_norm = norm_layer(args.decoder_embed_dim)
        else:
            self.encoder_to_decoder = None

        self.mask_token = nn.Parameter(torch.zeros(1, 1, args.decoder_embed_dim))
        trunc_normal_(self.mask_token, std=self.init_std)

        ### whether to use 'rescale' to init the weight, borrowed from beit.
        if not args.fix_init_weight:
            self.apply(self._init_weights)
        self._init_teacher()
        
        
    def _init_teacher(self):  
        # init the weights of teacher with those of backbone
        for param_encoder, param_teacher in zip(self.encoder.parameters(), self.teacher.parameters()):
            param_teacher.detach()
            param_teacher.data.copy_(param_encoder.data)
            param_teacher.requires_grad = False

    def momentum_update(self, base_momentum=0):
        """Momentum update of the teacher network."""
        for param_encoder, param_teacher in zip(self.encoder.parameters(),
                                                self.teacher.parameters()):
            param_teacher.data = param_teacher.data * base_momentum + \
                param_encoder.data * (1. - base_momentum)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
    
    '''
    Input shape:
        x: [bs, 3, 224, 224]
        bool_masked_pos: [bs, num_patch * num_patch]
    '''
    def forward(self, x, bool_masked_pos, return_all_tokens=None):
        batch_size = x.size(0)

        '''
        Encoder
        Output shape:
            [bs, num_visible + 1, C]
        '''
        x_unmasked = self.encoder(x, bool_masked_pos=bool_masked_pos)

        # encoder to decoder projection
        if self.encoder_to_decoder is not None:
            x_unmasked = self.encoder_to_decoder(x_unmasked)
            x_unmasked = self.encoder_to_decoder_norm(x_unmasked)

        '''
        Alignment constraint
        '''
        with torch.no_grad():
            latent_target = self.teacher(x, bool_masked_pos=(~bool_masked_pos))
            latent_target = latent_target[:, 1:, :] # remove class token
            if self.encoder_to_decoder is not None:
                latent_target = self.encoder_to_decoder_norm(self.encoder_to_decoder(latent_target.detach()))

            self.momentum_update(self.args.base_momentum)

        '''
        Latent contextual regressor and decoder
        '''
        b, num_visible_plus1, dim = x_unmasked.shape
        # remove class token
        x_unmasked = x_unmasked[:, 1:, :]

        num_masked_patches = self.num_patches - (num_visible_plus1-1)
        
        # generate position embeddings.
        pos_embed = self.encoder.build_2d_sincos_position_embedding(dim, use_cls_token=True).expand(batch_size, self.num_patches+1, dim).cuda(x_unmasked.device)

        # pos embed for masked patches
        pos_embed_masked = pos_embed[:,1:][bool_masked_pos].reshape(batch_size, -1, dim) 

        # pos embed for unmasked patches
        pos_embed_unmasked = pos_embed[:,1:][~bool_masked_pos].reshape(batch_size, -1, dim) 

        # masked embedding '''
        x_masked = self.mask_token.expand(batch_size, num_masked_patches, -1)

        logits, latent_pred = self.pretext_neck(x_masked, x_unmasked, pos_embed_masked, pos_embed_unmasked, bool_masked_pos)
        logits = logits.view(-1, logits.shape[2])

        return logits, latent_pred, latent_target


@register_model
def cae_small_patch16_224_8k_vocab(pretrained=False, **kwargs):
    model = VisionTransformerForMaskedImageModeling(
        patch_size=16, embed_dim=384, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), vocab_size=8192, **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.load(
            kwargs["init_ckpt"], map_location="cpu"
        )
        model.load_state_dict(checkpoint["model"])
    return model


@register_model
def cae_base_patch16_224_8k_vocab(pretrained=False, **kwargs):
    model = VisionTransformerForMaskedImageModeling(
        patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), vocab_size=8192, **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.load(
            kwargs["init_ckpt"], map_location="cpu"
        )
        model.load_state_dict(checkpoint["model"])
    return model


@register_model
def cae_large_patch16_224_8k_vocab(pretrained=False, **kwargs):
    model = VisionTransformerForMaskedImageModeling(
        patch_size=16, embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), vocab_size=8192, **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.load(
            kwargs["init_ckpt"], map_location="cpu"
        )
        model.load_state_dict(checkpoint["model"])
    return model