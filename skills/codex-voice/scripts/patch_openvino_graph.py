"""Rewrite Kokoro ONNX operations that Intel GPU OpenVINO cannot compile.

The Kokoro v1.0 export contains two linear rank-3 Resize nodes and one STFT
node whose output rank is omitted. Flatten the fixed batch/channel dimensions
so the 1-D interpolation uses OpenVINO's supported rank-2 path, and replace
the fixed STFT with an equivalent 2-D convolution using precomputed DFT
kernels.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper


def initializer_values(model: onnx.ModelProto) -> dict[str, np.ndarray]:
    return {
        initializer.name: numpy_helper.to_array(initializer)
        for initializer in model.graph.initializer
    }


def rank_of(value_info: onnx.ValueInfoProto | None) -> int | None:
    if value_info is None or not value_info.type.HasField("tensor_type"):
        return None
    tensor_type = value_info.type.tensor_type
    if not tensor_type.HasField("shape"):
        return None
    return len(tensor_type.shape.dim)


def node_attributes(node: onnx.NodeProto) -> dict[str, object]:
    return {
        attribute.name: helper.get_attribute_value(attribute)
        for attribute in node.attribute
    }


def dft_conv_weights(window: np.ndarray, frame_length: int) -> np.ndarray:
    """Return [real0, imag0, real1, imag1, ...] Conv2D kernels."""
    bins = frame_length // 2 + 1
    samples = np.arange(frame_length, dtype=np.float64)
    kernels: list[np.ndarray] = []
    for frequency in range(bins):
        phase = 2.0 * np.pi * frequency * samples / frame_length
        kernels.append(window * np.cos(phase))
        kernels.append(window * -np.sin(phase))
    return np.asarray(kernels, dtype=window.dtype)[:, np.newaxis, np.newaxis, :]


def patch_model(
    source: Path,
    destination: Path,
    *,
    patch_resize: bool = True,
) -> tuple[int, int]:
    model = onnx.load(str(source), load_external_data=False)
    values = initializer_values(model)
    value_infos = {
        value_info.name: value_info
        for value_info in list(model.graph.input)
        + list(model.graph.value_info)
        + list(model.graph.output)
    }

    stft_axes_name = "/codex_voice/openvino_stft_axes"
    model.graph.initializer.append(
        numpy_helper.from_array(
            np.asarray([1, 2], dtype=np.int64), stft_axes_name
        )
    )

    patched_nodes: list[onnx.NodeProto] = []
    resize_count = 0
    stft_count = 0

    for node in model.graph.node:
        attrs = node_attributes(node)
        is_linear_resize = (
            patch_resize
            and node.op_type == "Resize"
            and attrs.get("mode", b"nearest") == b"linear"
            and rank_of(value_infos.get(node.input[0])) == 3
            and len(node.input) >= 3
            and bool(node.input[2])
            and node.input[2] in values
        )
        if is_linear_resize:
            scales = np.asarray(values[node.input[2]])
            if scales.ndim != 1 or scales.size != 3:
                raise ValueError(f"Expected 3-D scales initializer for {node.name}")
            input_info = value_infos[node.input[0]].type.tensor_type.shape.dim
            leading_dimensions = [
                dimension.dim_value for dimension in input_info[:2]
            ]
            if len(leading_dimensions) != 2 or not all(leading_dimensions):
                raise ValueError(
                    f"Expected fixed batch and channel dimensions for {node.name}"
                )
            leading_size = int(np.prod(leading_dimensions))

            original_output = node.output[0]
            flattened_input = f"{node.name}/openvino_2d_input"
            resized_output = f"{node.name}/openvino_2d_output"
            input_shape_name = f"{node.name}/openvino_input_shape"
            output_shape_name = f"{node.name}/openvino_output_shape"
            scales_name = f"{node.name}/openvino_2d_scales"
            model.graph.initializer.extend(
                [
                    numpy_helper.from_array(
                        np.asarray([leading_size, -1], dtype=np.int64),
                        input_shape_name,
                    ),
                    numpy_helper.from_array(
                        np.asarray([*leading_dimensions, -1], dtype=np.int64),
                        output_shape_name,
                    ),
                    numpy_helper.from_array(
                        np.asarray([1, scales[2]], dtype=scales.dtype), scales_name
                    ),
                ]
            )

            patched_nodes.append(
                helper.make_node(
                    "Reshape",
                    [node.input[0], input_shape_name],
                    [flattened_input],
                    name=f"{node.name}/openvino_flatten",
                )
            )
            node.input[0] = flattened_input
            node.input[2] = scales_name
            node.output[0] = resized_output
            patched_nodes.append(node)
            patched_nodes.append(
                helper.make_node(
                    "Reshape",
                    [resized_output, output_shape_name],
                    [original_output],
                    name=f"{node.name}/openvino_restore",
                )
            )
            resize_count += 1
            continue

        if node.op_type == "STFT":
            if len(node.input) != 4 or attrs.get("onesided", 1) != 1:
                raise ValueError(f"Unsupported STFT signature for {node.name}")
            frame_step_value = values.get(node.input[1])
            window_value = values.get(node.input[2])
            frame_length_value = values.get(node.input[3])
            if (
                frame_step_value is None
                or window_value is None
                or frame_length_value is None
            ):
                raise ValueError(f"STFT parameters must be constant for {node.name}")

            frame_step = int(np.asarray(frame_step_value).item())
            frame_length = int(np.asarray(frame_length_value).item())
            window = np.asarray(window_value)
            if window.ndim != 1 or window.size != frame_length:
                raise ValueError(f"Unexpected STFT window for {node.name}")
            bins = frame_length // 2 + 1

            promoted_input = f"{node.name}/openvino_4d_input"
            conv_output = f"{node.name}/openvino_dft_output"
            packed_output = f"{node.name}/openvino_packed_output"
            weights_name = f"{node.name}/openvino_dft_weights"
            shape_name = f"{node.name}/openvino_output_shape"
            model.graph.initializer.extend(
                [
                    numpy_helper.from_array(
                        dft_conv_weights(window, frame_length), weights_name
                    ),
                    numpy_helper.from_array(
                        np.asarray([0, bins, 2, -1], dtype=np.int64), shape_name
                    ),
                ]
            )

            patched_nodes.extend(
                [
                    helper.make_node(
                        "Unsqueeze",
                        [node.input[0], stft_axes_name],
                        [promoted_input],
                        name=f"{node.name}/openvino_promote",
                    ),
                    helper.make_node(
                        "Conv",
                        [promoted_input, weights_name],
                        [conv_output],
                        name=f"{node.name}/openvino_dft_conv",
                        kernel_shape=[1, frame_length],
                        strides=[1, frame_step],
                        pads=[0, 0, 0, 0],
                    ),
                    helper.make_node(
                        "Reshape",
                        [conv_output, shape_name],
                        [packed_output],
                        name=f"{node.name}/openvino_pack",
                    ),
                    helper.make_node(
                        "Transpose",
                        [packed_output],
                        list(node.output),
                        name=f"{node.name}/openvino_layout",
                        perm=[0, 3, 1, 2],
                    ),
                ]
            )
            stft_count += 1
            continue

        patched_nodes.append(node)

    model.graph.ClearField("node")
    model.graph.node.extend(patched_nodes)
    used_inputs = {
        input_name
        for node in model.graph.node
        for input_name in node.input
        if input_name
    }
    kept_initializers = [
        initializer
        for initializer in model.graph.initializer
        if initializer.name in used_inputs
    ]
    model.graph.ClearField("initializer")
    model.graph.initializer.extend(kept_initializers)

    expected_resize_count = 2 if patch_resize else 0
    if resize_count != expected_resize_count or stft_count != 1:
        raise ValueError(
            f"Expected {expected_resize_count} linear Resize nodes and 1 STFT node; got "
            f"{resize_count} and {stft_count}"
        )
    onnx.checker.check_model(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    onnx.save_model(model, str(destination), save_as_external_data=False)
    return resize_count, stft_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--keep-linear-resize",
        action="store_true",
        help="leave rank-3 interpolation for OpenVINO HETERO CPU routing",
    )
    args = parser.parse_args()
    resize_count, stft_count = patch_model(
        args.source,
        args.destination,
        patch_resize=not args.keep_linear_resize,
    )
    print(
        f"patched {resize_count} linear rank-3 Resize nodes and "
        f"{stft_count} STFT node -> {args.destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
