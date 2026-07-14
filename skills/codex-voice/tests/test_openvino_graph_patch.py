from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

try:
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper
except ModuleNotFoundError:
    onnx = None
    ort = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

if onnx is not None:
    import patch_openvino_graph


@unittest.skipIf(onnx is None, "OpenVINO graph tests require onnx")
class OpenVinoGraphPatchTests(unittest.TestCase):
    def test_dft_conv_weights_match_rfft(self) -> None:
        frame_length = 20
        samples = np.arange(frame_length, dtype=np.float32)
        window = 0.5 - 0.5 * np.cos(2.0 * np.pi * samples / frame_length)
        signal = (
            np.random.default_rng(42)
            .normal(size=frame_length)
            .astype(np.float32)
        )

        weights = patch_openvino_graph.dft_conv_weights(window, frame_length)
        components = weights[:, 0, 0, :] @ signal
        actual = components[0::2] + 1j * components[1::2]
        expected = np.fft.rfft(signal * window)

        np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=2e-5)

    def test_patch_preserves_resize_and_stft_outputs(self) -> None:
        frame_length = 20
        frame_step = 5
        samples = np.arange(frame_length, dtype=np.float32)
        window = 0.5 - 0.5 * np.cos(2.0 * np.pi * samples / frame_length)
        nodes = [
            helper.make_node(
                "Resize",
                ["features", "", "up_scales"],
                ["upsampled"],
                name="linear_up",
                mode="linear",
                coordinate_transformation_mode="half_pixel",
            ),
            helper.make_node(
                "Resize",
                ["upsampled", "", "down_scales"],
                ["resized"],
                name="linear_down",
                mode="linear",
                coordinate_transformation_mode="half_pixel",
            ),
            helper.make_node(
                "STFT",
                ["signal", "frame_step", "window", "frame_length"],
                ["spectrum"],
                name="stft",
                onesided=1,
            ),
        ]
        initializers = [
            numpy_helper.from_array(
                np.asarray([1.0, 1.0, 2.0], dtype=np.float32), "up_scales"
            ),
            numpy_helper.from_array(
                np.asarray([1.0, 1.0, 0.5], dtype=np.float32), "down_scales"
            ),
            numpy_helper.from_array(
                np.asarray(frame_step, dtype=np.int64), "frame_step"
            ),
            numpy_helper.from_array(window.astype(np.float32), "window"),
            numpy_helper.from_array(
                np.asarray(frame_length, dtype=np.int64), "frame_length"
            ),
        ]
        graph = helper.make_graph(
            nodes,
            "openvino_patch_fixture",
            [
                helper.make_tensor_value_info(
                    "features", TensorProto.FLOAT, [1, 9, None]
                ),
                helper.make_tensor_value_info("signal", TensorProto.FLOAT, [1, None]),
            ],
            [
                helper.make_tensor_value_info(
                    "resized", TensorProto.FLOAT, [1, 9, None]
                ),
                helper.make_tensor_value_info(
                    "spectrum", TensorProto.FLOAT, [1, None, 11, 2]
                ),
            ],
            initializer=initializers,
            value_info=[
                helper.make_tensor_value_info(
                    "upsampled", TensorProto.FLOAT, [1, 9, None]
                )
            ],
        )
        model = helper.make_model(
            graph,
            opset_imports=[helper.make_opsetid("", 20)],
            ir_version=9,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.onnx"
            destination = root / "patched.onnx"
            onnx.save(model, source)
            counts = patch_openvino_graph.patch_model(source, destination)

            self.assertEqual(counts, (2, 1))
            patched = onnx.load(destination, load_external_data=False)
            self.assertFalse(
                any(node.op_type == "STFT" for node in patched.graph.node)
            )
            self.assertEqual(
                sum(
                    node.name.endswith("/openvino_dft_conv")
                    for node in patched.graph.node
                ),
                1,
            )

            original_session = ort.InferenceSession(
                str(source), providers=["CPUExecutionProvider"]
            )
            patched_session = ort.InferenceSession(
                str(destination), providers=["CPUExecutionProvider"]
            )
            rng = np.random.default_rng(7)
            inputs = {
                "features": rng.normal(size=(1, 9, 32)).astype(np.float32),
                "signal": rng.normal(size=(1, 137)).astype(np.float32),
            }
            expected = original_session.run(None, inputs)
            actual = patched_session.run(None, inputs)

            np.testing.assert_allclose(actual[0], expected[0], rtol=1e-5, atol=1e-5)
            np.testing.assert_allclose(actual[1], expected[1], rtol=1e-5, atol=2e-5)


if __name__ == "__main__":
    unittest.main()
