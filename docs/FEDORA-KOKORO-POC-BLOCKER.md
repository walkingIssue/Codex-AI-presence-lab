# Fedora Kokoro Arc POC status

The Fedora voice-seam POC is intentionally stopped at the GPU capability gate.

## Audit result

- Fork: `https://github.com/walkingIssue/kokoro-onnx-intel-arc`
- Branch: `intel-arc-directml`
- Audited commit: `9dd55a72289e8da11369aa553f0ccc201ef3e13b`
- Hardware: Intel Arc A770 (`DG2`)
- Host: Fedora Linux 44, x86_64

The fork's `docs/intel-arc-directml.md` describes the workaround as a Windows
path and selects ONNX Runtime's `DmlExecutionProvider`. Its package metadata
does not provide a Linux DML implementation; the normal Linux GPU extra is
`onnxruntime-gpu`, which does not expose DirectML.

An isolated Fedora ONNX Runtime probe (`onnxruntime==1.27.0`) reported only:

```text
['AzureExecutionProvider', 'CPUExecutionProvider']
```

Therefore the branch does not claim a working Fedora Arc GPU path. The setup
command now rejects `--directml` on non-Windows hosts instead of creating a
runtime that silently falls back to CPU. A future Fedora Arc implementation
must supply or select a Linux-supported provider (for example, an Intel GPU
provider) before the GPU gate can pass.

The platform-independent virtual-environment path handling was corrected in
the setup, watcher, toggle, and configuration scripts so the CPU seam can be
validated independently when a compatible Python runtime is available.
