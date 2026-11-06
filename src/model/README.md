# 模型目录说明

这里存放图片预处理、已有的 CNN 图片编码器，以及供自己实现的图片 Transformer Encoder 框架。
Transformer 的各个模块已经定义接口，具体计算由各文件中的中文 TODO 指引。

## 当前状态

- `input.py` 中的 `prepare_images` 和 `ImageInputModel` 已有实现。
- 本目录中的 `TransformerConfig` 可以创建，Transformer 模型类可以实例化；其所有 `forward` 都会抛出 `NotImplementedError`。
- 根目录的 [train.py](../../train.py) 是训练入口占位，训练流程待实现。

下面的数据流、形状和掩码效果是 Transformer 的待实现约定。

## 文件划分

```text
model/
├── README.md
├── __init__.py
├── input.py
├── config.py
├── patch_embedding.py
├── position_encoding.py
├── attention.py
├── feed_forward.py
├── encoder.py
└── model.py
```

| 文件 | 类或函数 | 职责 |
| --- | --- | --- |
| [input.py](input.py) | `prepare_images` | 读取图片、纠正方向、转换 RGB、统一尺寸并生成像素张量 |
| [input.py](input.py) | `ImageInputModel` | 已有的 CNN 图片特征提取器，可独立使用 |
| [config.py](config.py) | `TransformerConfig` | 集中保存图片尺寸、向量维度、头数、层数等配置 |
| [patch_embedding.py](patch_embedding.py) | `PatchEmbedding` | 图片切块，将每块展平并线性映射为 token 向量 |
| [position_encoding.py](position_encoding.py) | `PositionEncoding` | 加入图片内部的块位置，以及组内的图片位置 |
| [attention.py](attention.py) | `MultiHeadSelfAttention` | 计算 Q/K/V、多头拆分、注意力权重及各头合并 |
| [feed_forward.py](feed_forward.py) | `FeedForward` | 对每个 token 分别执行前馈网络 |
| [encoder.py](encoder.py) | `TransformerEncoderLayer` | 将注意力、前馈网络、LayerNorm 和残差连接组合成一层 |
| [encoder.py](encoder.py) | `TransformerEncoder` | 堆叠多个参数独立的 Encoder 层，并做最终归一化 |
| [model.py](model.py) | `ImageTransformer` | 串联图片输入层与 Encoder，处理形状、掩码和特征汇总 |
| [__init__.py](__init__.py) | 公共导出 | 统一提供配置和模型类的导入入口 |

各 Transformer 模块统一采用 `__init__(config: TransformerConfig)`。
`ImageTransformer` 直接接收像素张量，图片特征通过切块和 Transformer 学习。

## 数据流与形状

`prepare_images` 返回 `[N, 3, H, W]`，表示一个样本组中的 N 张图片。
多组样本需要整理为 `[B, N, 3, H, W]`；不同组的图片数量不同时，需要补齐并提供掩码。

```text
图片路径或 PIL 图片列表
    ↓ prepare_images
像素张量 [N, 3, H, W]
    ↓ ImageTransformer：统一批次维度
[B, N, 3, H, W]
    ↓ 合并组数和图片数，M = B*N
[M, 3, H, W]
    ↓ PatchEmbedding：切块、展平、线性映射
[M, P, D]
    ↓ 恢复图片分组
[B, N, P, D]
    ↓ PositionEncoding：加入块位置和图片位置
[B, N, P, D]
    ↓ 展开为同一组内的 token 序列，L = N*P
[B, L, D]
    ↓ TransformerEncoder：有效图片块之间交换信息
[B, L, D]
    ↓ 恢复图片维度，对每张图片的 P 个块取平均
[B, N, D]
    ↓ 根据 pooling 选择输出方式
逐张特征 [B, N, D] 或整组特征 [B, D]
```

| 符号 | 含义 |
| --- | --- |
| `B` | 样本组数；单组输入会补上 `B=1` |
| `N` | 每组图片位置数，包含补齐位置 |
| `C` | 通道数；`prepare_images` 固定输出 RGB 三通道 |
| `H, W` | 图片高和宽；框架约定使用配置指定的正方形尺寸 |
| `M` | 合并组数后的图片总数，`M=B*N` |
| `P` | 每张图片的块数，`P=(image_size / patch_size)²` |
| `D` | 每个 token 的向量长度，即 `embed_dim` |
| `L` | 一组的序列长度，`L=N*P` |

默认 224×224 的图片按 16×16 切块，每张得到 196 个 token；两张图片组成一组时，序列长度为 392。
注意力矩阵的大小随序列长度的平方增长，实现时可先用较小图片验证。

## 掩码与输出

所有掩码统一采用布尔类型：`True` 表示有效，`False` 表示补齐。

| 参数 | 形状 | 使用位置 |
| --- | --- | --- |
| `image_mask` | `[B, N]` | `ImageTransformer` 入口，每个标记对应整张图片 |
| `image_mask` | `[N]` | 仅当原始图片输入为单组四维张量时支持 |
| `token_mask` | `[B, L]` | 注意力和 Encoder，每个标记对应一个图片块 |

完整模型负责将每张图片的标记扩展到其全部 P 个块。不传掩码时，默认所有位置有效。
每组至少保留一张有效图片；补齐位置应在注意力计算中被忽略，最终特征应为零。

- `pooling="none"`：返回 `[B, N, D]`，每张有效图片的特征已经包含组内其他图片的信息。
- `pooling="mean"`：对有效图片的特征取平均，返回 `[B, D]`。

无论输入是否包含批次维度，输出都保留 B 维度。
完整模型返回特征，分类或回归等任务输出层在训练入口中另行组合。

## 配置与导入

从项目根目录运行 Python，可以使用统一入口导入：

```python
from src.model.input import prepare_images
from src.model import ImageTransformer, TransformerConfig

config = TransformerConfig()
model = ImageTransformer(config)

# 当前只创建了框架对象，没有创建实际网络层。
# 待各模块实现后，再调用 model(images, image_mask=mask)。
```

默认配置见 [config.py](config.py)：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `image_size` | `224` | 输入图片边长 |
| `patch_size` | `16` | 图片块边长 |
| `in_channels` | `3` | 图片通道数 |
| `embed_dim` | `256` | token 向量长度 |
| `num_heads` | `8` | 注意力头数 |
| `depth` | `4` | Encoder 层数 |
| `mlp_dim` | `1024` | 前馈网络中间层维度 |
| `dropout` | `0.1` | 随机丢弃概率 |
| `max_images` | `10` | 每组图片位置数上限，包含补齐位置 |
| `pooling` | `"mean"` | 输出整组平均特征或逐张特征 |

配置类目前仅保存参数。实现时需要校验正整数、尺寸整除、`embed_dim` 能被 `num_heads` 整除，
以及 dropout 的范围。图片预处理的 `image_size` 应与模型配置一致。
`prepare_images` 当前固定输出三通道，并限制每组 1～10 张；更改模型配置不会自动修改预处理约束。

## 建议实现顺序

1. 检查 `config.py`，确定各参数与合法范围。
2. 实现 `patch_embedding.py`，验证图片块的数量、顺序和输出形状。
3. 实现 `position_encoding.py`，区分块位置与图片位置。
4. 实现 `attention.py`，先理解单头，再加入多头和掩码。
5. 实现 `feed_forward.py`，保持输入、输出维度一致。
6. 实现 `encoder.py` 中的单层结构，再堆叠多层。
7. 实现 `model.py`，串联模块，扩展掩码并汇总特征。
8. 在 [train.py](../../train.py) 中按实际任务加入数据、输出层、损失函数和训练循环。

每一步都可以用小尺寸随机张量检查形状，再检查掩码隔离和反向传播。
现有 [test_input.py](../../tests/test_input.py) 验证图片预处理和 CNN，Transformer 的测试随实现补充。
