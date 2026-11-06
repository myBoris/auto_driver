"""将 1～10 张图片编码为可训练的 PyTorch 特征。"""

from collections.abc import Sequence
from os import PathLike
from typing import Literal

import torch
from PIL import Image, ImageOps
from torch import Tensor, nn
from torchvision import transforms

MAX_IMAGES = 10
ImageSource = str | PathLike[str] | Image.Image


def prepare_images(images: Sequence[ImageSource], image_size: int = 224) -> Tensor:
    """读取 1～10 个图片路径或 PIL 图片，返回 float32 类型的 [N, 3, H, W] 张量。

    图片统一转换为 RGB 并缩放为正方形，像素值范围为 [0, 1]。
    返回的张量位于 CPU，调用模型前需将其移到模型所在的设备。
    函数不会关闭调用方传入的 PIL 图片对象。
    """
    if isinstance(images, (str, bytes, PathLike, Image.Image)) or not isinstance(
        images, Sequence
    ):
        raise TypeError("images must be a sequence of image paths or PIL images.")
    if not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError(f"Each sample must contain 1 to {MAX_IMAGES} images.")
    if isinstance(image_size, bool) or not isinstance(image_size, int) or image_size < 1:
        raise ValueError("image_size must be a positive integer.")

    transform = transforms.Compose(
        [transforms.Resize((image_size, image_size)), transforms.ToTensor()]
    )
    # 保存每张图片处理后的张量
    tensors = []

    for image in images:
        # 如果输入是文件路径（字符串或 PathLike 对象）
        if isinstance(image, (str, PathLike)):
            # 打开图片，处理结束后自动关闭文件
            with Image.open(image) as opened_image:
                # 根据 EXIF 信息纠正图片方向，转为 RGB，
                # 再执行 transform，将处理后的张量加入列表
                tensors.append(
                    transform(ImageOps.exif_transpose(opened_image).convert("RGB"))
                )

        # 如果输入已经是 PIL 图片对象，直接处理
        elif isinstance(image, Image.Image):
            tensors.append(
                transform(ImageOps.exif_transpose(image).convert("RGB"))
            )

        # 不支持其他输入类型
        else:
            raise TypeError("Each image must be a file path or a PIL image.")

    # 将图片张量堆叠成一个批次；要求各张量形状相同且列表非空
    # 通常输出形状为 [图片数量, 通道数, 高度, 宽度]
    return torch.stack(tensors)