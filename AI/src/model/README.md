# 视觉与驾驶多任务模型

本目录实现了可训练的图片 Transformer，以及车道边界、方向类别、转向/油门/刹车和 Xbox 按钮的联合预测。模型只进行张量计算，驾驶操作由 AI 服务通过 Center 执行。

## 模型组成

| 文件 | 职责 |
| --- | --- |
| `config.py` | 校验图片大小、patch、特征维度、头数、层数及 dropout |
| `patch_embedding.py` | 不重叠卷积切块，等价于每块像素的线性映射 |
| `position_encoding.py` | 图片内 patch 与时间窗口内图片位置编码 |
| `attention.py` | Q/K/V 投影、缩放点积多头注意力、有效位置掩码 |
| `feed_forward.py` | 两层 GELU 前馈网络 |
| `encoder.py` | Pre-LayerNorm、残差、多层堆叠和最终归一化 |
| `model.py` | 图片组编码、补齐隔离及特征汇总 |
| `driving.py` | 可序列化结构配置及五个驾驶任务头 |
| `input.py` | 原有图片路径/PIL 图片的 RGB 张量转换工具 |

训练和在线推理应共同使用 `src.data.preprocessing.ObservationPreprocessor`，并读取同一检查点保存的预处理配置。不要只在训练或推理的一侧添加裁剪或不同的归一化。

## 输入与时间窗口

`ImageTransformer` 和 `DrivingModel` 均接收：

- 单个样本的 `[N, 3, H, W]`；单组输入仍保留输出批次维 `B=1`。
- 一个批次的 `[B, N, 3, H, W]`。
- 可选布尔掩码 `image_mask[B,N]`；仅单组四维输入也接受 `[N]`。

`True` 表示有效帧，`False` 表示时间窗口的补齐位置。每个样本至少需要一张有效图片，`N` 不得超过 `max_images`。补齐像素在进入切块层前清零，即使补齐位置含 NaN，也不会污染有效特征或梯度。有效像素必须有限。

图片按 patch 切成 token，加入空间与图片顺序位置编码，再在同一时间窗口内计算注意力。`ImageTransformer(pooling="mean")` 输出 `[B,D]`；`pooling="none"` 输出 `[B,N,D]`，补齐帧的特征为零。驾驶多任务模型要求 RGB 输入与 `mean` pooling。

## 多任务输出

```python
from src.model import DrivingModel, DrivingModelConfig

config = DrivingModelConfig()
model = DrivingModel(config)
outputs = model(images, image_mask)
```

| 输出键 | 形状 | 含义 |
| --- | --- | --- |
| `controls` | `[B,3]` | 转向 `[-1,1]`、油门 `[0,1]`、刹车 `[0,1]` |
| `button_logits` | `[B,15]` | 按 `config.button_names` 排列的按钮 logits，推理时 sigmoid |
| `lane_points` | `[B,K,2]` | 固定 y 锚点处的左右边界 x 坐标，归一化到完整画面 `[0,1]` |
| `lane_presence_logit` | `[B]` | 当前目标车道可见性的二分类 logit |
| `direction_logits` | `[B,3]` | 左转、直行、右转的分类 logits |

`lane_y` 默认是从近到远的 `(0.95,0.85,0.75,0.65,0.55,0.45)`。左右边界通过有序参数化保持 `left < right`，不预测额外的中间分隔线。页面需要的道路多边形和中心点由推理侧从两个边界生成；页面仍只绘制道路区域及方向箭头。

方向分类概率是模型对左/直/右类别的输出，不能解释为真实车辆动作完成率。按钮头按数据标签训练，离合使用 LB 开关，不是额外的模拟轴。

## 检查点与验证

`DrivingModelConfig.to_dict()` 将结构配置转为 JSON 可序列化的字典；`DrivingModelConfig.from_dict()` 重建同一结构。训练检查点须同时保存模型参数、结构配置、预处理规则、训练任务及数据来源。只有车道标签时不能把未训练的操作头作为驾驶策略使用。

在 AI 目录执行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_model -v
```

测试覆盖完整反向传播、掩码 NaN 隔离、单组/批次形状、边界和控制输出范围，以及配置与参数重载。
