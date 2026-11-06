"""Verify CUDA training and torchvision with locally generated data."""

import platform

import torch
import torchvision
from torchvision.ops import nms


def main() -> None:
    print(f"Python: {platform.python_version()}")
    print(f"PyTorch: {torch.__version__}")
    print(f"torchvision: {torchvision.__version__}")
    print(f"CUDA runtime: {torch.version.cuda}")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Check the NVIDIA driver and GPU environment.")

    device = torch.device("cuda")
    properties = torch.cuda.get_device_properties(device)
    print(f"GPU: {properties.name} ({properties.total_memory / 1024**3:.1f} GiB)")
    torch.manual_seed(42)
    inputs = torch.randn(256, 16, device=device)
    targets = inputs.sum(dim=1, keepdim=True)
    model = torch.nn.Linear(16, 1).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.05)
    loss_fn = torch.nn.MSELoss()
    initial_loss = loss_fn(model(inputs), targets).item()

    for _ in range(100):
        optimizer.zero_grad(set_to_none=True)
        loss = loss_fn(model(inputs), targets)
        loss.backward()
        optimizer.step()

    with torch.no_grad():
        final_loss = loss_fn(model(inputs), targets).item()
    if not (0 <= final_loss < initial_loss):
        raise RuntimeError(f"Training did not converge: {initial_loss} -> {final_loss}")

    boxes = torch.tensor([[0, 0, 10, 10], [1, 1, 9, 9]], dtype=torch.float32, device=device)
    scores = torch.tensor([0.9, 0.8], device=device)
    if nms(boxes, scores, 0.5).tolist() != [0]:
        raise RuntimeError("torchvision CUDA NMS check failed.")
    torch.cuda.synchronize()
    print(f"Training loss: {initial_loss:.6f} -> {final_loss:.6f}")
    print("PASS: GPU forward/backward, optimizer, and torchvision CUDA operations.")


if __name__ == "__main__":
    main()
