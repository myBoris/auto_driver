"""验证真实可微模型、补齐隔离和检查点配置，不启动游戏输入。"""

import json
import unittest

import torch

from src.model import (DrivingModel, DrivingModelConfig, ImageTransformer,
                       MultiHeadSelfAttention, TransformerConfig, TransformerEncoder)


def small_config(**options):
    return TransformerConfig(**{
        "image_size": 16, "patch_size": 8, "embed_dim": 16, "num_heads": 4,
        "depth": 2, "mlp_dim": 32, "dropout": 0, "max_images": 3, **options,
    })


class TransformerModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = torch.get_num_threads()
        torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.threads)

    def setUp(self):
        torch.manual_seed(8)

    def test_masked_nan_pixels_cannot_change_valid_features_or_parameter_gradients(self):
        model = ImageTransformer(small_config(pooling="none")).eval()
        images = torch.rand(2, 3, 3, 16, 16)
        mask = torch.tensor([[True, False, True], [True, True, False]])
        clean = images.masked_fill(~mask[:, :, None, None, None], 0)
        poisoned = images.masked_fill(~mask[:, :, None, None, None], float("nan")).requires_grad_()
        expected = model(clean, mask)
        actual = model(poisoned, mask)
        torch.testing.assert_close(actual, expected)
        self.assertTrue(torch.equal(actual[~mask], torch.zeros_like(actual[~mask])))
        (actual.square().sum()).backward()
        self.assertTrue(torch.isfinite(poisoned.grad).all())
        self.assertTrue(torch.equal(poisoned.grad[~mask], torch.zeros_like(poisoned.grad[~mask])))
        self.assertTrue(all(parameter.grad is not None and torch.isfinite(parameter.grad).all()
                            for parameter in model.parameters()))

    def test_group_and_batch_contracts_and_masked_mean_agree(self):
        config = small_config(pooling="none")
        detailed = ImageTransformer(config).eval()
        pooled = ImageTransformer(small_config()).eval()
        pooled.load_state_dict(detailed.state_dict())
        images = torch.rand(3, 3, 16, 16)
        mask = torch.tensor([True, False, True])
        per_image = detailed(images, mask)
        self.assertEqual(per_image.shape, (1, 3, 16))
        torch.testing.assert_close(pooled(images, mask), per_image[:, [0, 2]].mean(1))
        torch.testing.assert_close(detailed(images, mask), detailed(images.unsqueeze(0), mask.unsqueeze(0)))

    def test_direct_attention_and_encoder_ignore_masked_tokens_and_return_zero_padding(self):
        tokens = torch.randn(2, 5, 16)
        mask = torch.tensor([[True, True, False, False, False], [True, False, True, False, True]])
        poisoned = tokens.masked_fill(~mask.unsqueeze(-1), float("nan"))
        for module in (MultiHeadSelfAttention(small_config()), TransformerEncoder(small_config())):
            module.eval()
            output = module(poisoned, mask)
            torch.testing.assert_close(output, module(tokens.masked_fill(~mask.unsqueeze(-1), 0), mask))
            self.assertTrue(torch.equal(output[~mask], torch.zeros_like(output[~mask])))

    def test_invalid_configuration_and_input_are_rejected(self):
        for value in ({"image_size": 15}, {"embed_dim": 15}, {"dropout": float("nan")},
                      {"max_images": 0}, {"depth": True}, {"pooling": "bad"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                small_config(**value)
        model = ImageTransformer(small_config())
        images = torch.rand(2, 3, 16, 16)
        for invalid_mask in (torch.tensor([False, False]), torch.ones(2), torch.ones(2, 2, dtype=torch.bool)):
            with self.subTest(mask=invalid_mask), self.assertRaises(ValueError):
                model(images, invalid_mask)
        for invalid in (torch.rand(2, 3, 15, 16), torch.ones(2, 3, 16, 16, dtype=torch.long),
                        torch.full((2, 3, 16, 16), float("nan"))):
            with self.assertRaises(ValueError):
                model(invalid)

    def test_multitask_outputs_obey_ranges_have_finite_gradients_and_reload(self):
        config = DrivingModelConfig(encoder=small_config())
        model = DrivingModel(config)
        images = torch.rand(4, 2, 3, 16, 16)
        result = model(images)
        self.assertEqual(result["controls"].shape, (4, 3))
        self.assertEqual(result["button_logits"].shape, (4, 15))
        self.assertEqual(result["lane_points"].shape, (4, 6, 2))
        self.assertEqual(result["lane_presence_logit"].shape, (4,))
        self.assertEqual(result["direction_logits"].shape, (4, 3))
        self.assertTrue((result["controls"][:, 0].abs() <= 1).all())
        self.assertTrue(((result["controls"][:, 1:] >= 0) & (result["controls"][:, 1:] <= 1)).all())
        lanes = result["lane_points"]
        self.assertTrue(((lanes >= 0) & (lanes <= 1)).all())
        self.assertTrue((lanes[:, :, 0] < lanes[:, :, 1]).all())
        loss = sum(tensor.square().mean() for tensor in result.values())
        loss.backward()
        self.assertTrue(all(parameter.grad is not None and torch.isfinite(parameter.grad).all()
                            for parameter in model.parameters()))
        clone = DrivingModel(DrivingModelConfig.from_dict(json.loads(json.dumps(config.to_dict()))))
        clone.load_state_dict(model.state_dict())
        model.eval(); clone.eval()
        for key, expected in model(images).items():
            torch.testing.assert_close(clone(images)[key], expected)

    def test_driving_configuration_rejects_incompatible_geometry_and_button_orders(self):
        config = DrivingModelConfig()
        self.assertEqual(DrivingModelConfig.from_dict(config.to_dict()), config)
        for options in ({"lane_y": (.3, .6)}, {"lane_y": (0.5, 0.5)}, {"lane_y": (float("nan"), .1)},
                        {"button_names": ("LB", "LB")}, {"button_names": ("unknown",)},
                        {"encoder": small_config(pooling="none")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DrivingModelConfig(**options)


if __name__ == "__main__":
    unittest.main()
