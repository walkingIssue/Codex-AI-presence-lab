from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper
except ModuleNotFoundError:
    np = None
    onnx = None
    ort = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

if onnx is not None:
    import split_openvino_graph


@unittest.skipIf(onnx is None, "OpenVINO graph tests require ONNX Runtime")
class OpenVinoGraphSplitTests(unittest.TestCase):
    def test_split_preserves_cpu_output_at_albert_boundary(self) -> None:
        boundary = split_openvino_graph.BERT_BOUNDARY
        nodes = [
            helper.make_node(
                "Cast",
                ["tokens"],
                ["token_features"],
                name="token_features",
                to=TensorProto.FLOAT,
            ),
            helper.make_node(
                "LayerNormalization",
                ["token_features", "layer_scale", "layer_bias"],
                [boundary],
                name=(
                    "/encoder/bert/encoder/albert_layer_groups.0/"
                    "albert_layers.0/full_layer_layer_norm_11/LayerNormalization"
                ),
                axis=-1,
            ),
            helper.make_node(
                "MatMul",
                [boundary, "projection"],
                ["bert_projection"],
                name="/encoder/bert_encoder/MatMul",
            ),
            helper.make_node(
                "Cast",
                ["tokens"],
                ["tail_tokens"],
                name="tail_tokens",
                to=TensorProto.FLOAT,
            ),
            helper.make_node(
                "ReduceSum",
                ["tail_tokens", "sum_axes"],
                ["token_sum"],
                name="token_sum",
                keepdims=1,
            ),
            helper.make_node(
                "Add", ["bert_projection", "style"], ["styled"]
            ),
            helper.make_node("Add", ["styled", "token_sum"], ["timed"]),
            helper.make_node("Add", ["timed", "speed"], ["audio"]),
        ]
        initializers = [
            numpy_helper.from_array(
                np.ones(3, dtype=np.float32), "layer_scale"
            ),
            numpy_helper.from_array(
                np.zeros(3, dtype=np.float32), "layer_bias"
            ),
            numpy_helper.from_array(
                np.ones((3, 1), dtype=np.float32), "projection"
            ),
            numpy_helper.from_array(
                np.asarray([1], dtype=np.int64), "sum_axes"
            ),
        ]
        graph = helper.make_graph(
            nodes,
            "openvino_split_fixture",
            [
                helper.make_tensor_value_info(
                    "tokens", TensorProto.INT64, [1, 3]
                ),
                helper.make_tensor_value_info(
                    "style", TensorProto.FLOAT, [1, 1]
                ),
                helper.make_tensor_value_info(
                    "speed", TensorProto.FLOAT, [1]
                ),
            ],
            [
                helper.make_tensor_value_info(
                    "audio", TensorProto.FLOAT, [1, 1]
                )
            ],
            initializer=initializers,
            value_info=[
                helper.make_tensor_value_info(
                    boundary, TensorProto.FLOAT, [1, 3]
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
            bert = root / "bert.onnx"
            tail = root / "tail.onnx"
            onnx.save(model, source)
            counts = split_openvino_graph.split_model(source, bert, tail)

            self.assertGreater(counts[0], 0)
            self.assertGreater(counts[1], 0)
            bert_model = onnx.load(bert, load_external_data=False)
            tail_model = onnx.load(tail, load_external_data=False)
            self.assertEqual(
                [value.name for value in bert_model.graph.input], ["tokens"]
            )
            self.assertEqual(
                [value.name for value in tail_model.graph.input],
                [boundary, "tokens", "style", "speed"],
            )

            inputs = {
                "tokens": np.asarray([[1, 2, 3]], dtype=np.int64),
                "style": np.asarray([[0.5]], dtype=np.float32),
                "speed": np.asarray([1.25], dtype=np.float32),
            }
            full_session = ort.InferenceSession(
                str(source), providers=["CPUExecutionProvider"]
            )
            bert_session = ort.InferenceSession(
                str(bert), providers=["CPUExecutionProvider"]
            )
            tail_session = ort.InferenceSession(
                str(tail), providers=["CPUExecutionProvider"]
            )
            expected = full_session.run(None, inputs)[0]
            encoded = bert_session.run(None, {"tokens": inputs["tokens"]})[0]
            actual = tail_session.run(
                None,
                {boundary: encoded, **inputs},
            )[0]
            np.testing.assert_allclose(actual, expected, rtol=0, atol=0)


if __name__ == "__main__":
    unittest.main()
