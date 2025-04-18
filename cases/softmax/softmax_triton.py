# Refer to: https://github.com/triton-lang/triton-cpu/blob/main/python/tutorials/02-fused-softmax-cpu.py

import os
import time
import numpy as np
import torch

import triton
import triton.language as tl

USE_GPU = False


@torch.jit.script
def naive_softmax(x):
    """Compute row-wise softmax of X using native pytorch

    We subtract the maximum element in order to avoid overflows. Softmax is invariant to
    this shift.
    """
    # read  MN elements ; write M  elements
    x_max = x.max(dim=1)[0]
    # read MN + M elements ; write MN elements
    z = x - x_max[:, None]
    # read  MN elements ; write MN elements
    numerator = torch.exp(z)
    # read  MN elements ; write M  elements
    denominator = numerator.sum(dim=1)
    # read MN + M elements ; write MN elements
    ret = numerator / denominator[:, None]
    # in total: read 5MN + 2M elements ; wrote 3MN + 2M elements
    return ret


@triton.jit
def softmax_kernel(
    output_ptr,
    input_ptr,
    input_row_stride,
    output_row_stride,
    n_cols,
    BLOCK_SIZE: tl.constexpr,
):
    # The rows of the softmax are independent, so we parallelize across those
    row_idx = tl.program_id(0)
    # The stride represents how much we need to increase the pointer to advance 1 row
    row_start_ptr = input_ptr + row_idx * input_row_stride
    # The block size is the next power of two greater than n_cols, so we can fit each
    # row in a single block
    col_offsets = tl.arange(0, BLOCK_SIZE)
    input_ptrs = row_start_ptr + col_offsets
    # Load the row into SRAM, using a mask since BLOCK_SIZE may be > than n_cols
    row = tl.load(input_ptrs, mask=col_offsets < n_cols, other=-float("inf"))
    # Subtract maximum for numerical stability
    row_minus_max = row - tl.max(row, axis=0)
    # Note that exponentiation in Triton is fast but approximate (i.e., think __expf in CUDA)
    numerator = tl.exp(row_minus_max)
    denominator = tl.sum(numerator, axis=0)
    softmax_output = numerator / denominator
    # Write back output to DRAM
    output_row_start_ptr = output_ptr + row_idx * output_row_stride
    output_ptrs = output_row_start_ptr + col_offsets
    tl.store(output_ptrs, softmax_output, mask=col_offsets < n_cols)


# %%
# We can create a helper function that enqueues the kernel and its (meta-)arguments for any given input tensor.
def softmax(x, y=None, num_threads=0):
    n_rows, n_cols = x.shape
    # The block size is the smallest power of two greater than the number of columns in `x`
    BLOCK_SIZE = triton.next_power_of_2(n_cols)
    # Another trick we can use is to ask the compiler to use more threads per row by
    # increasing the number of warps (`num_warps`) over which each row is distributed.
    # You will see in the next tutorial how to auto-tune this value in a more natural
    # way so you don't have to come up with manual heuristics yourself.
    num_warps = 4
    if BLOCK_SIZE >= 2048:
        num_warps = 8
    if BLOCK_SIZE >= 4096:
        num_warps = 16
    # Allocate output
    if y is None:
        y = torch.empty_like(x)
    # Enqueue kernel. The 1D launch grid is simple: we have one kernel instance per row of
    # the input matrix
    softmax_kernel[(n_rows,)](
        y,
        x,
        x.stride(0),
        y.stride(0),
        n_cols,
        num_warps=num_warps,
        BLOCK_SIZE=BLOCK_SIZE,
        num_threads=num_threads,
    )
    return y


def benchmark_triton(shape, a_np, axis=-1, parallel=True):
    os.environ["TRITON_CPU_BACKEND"] = "1"
    os.environ["TRITON_CPU_MAX_THREADS"] = "0" if parallel else "1"

    a = torch.tensor(a_np, device="cpu", dtype=torch.float32)
    assert a.is_contiguous(), "Matrix A must be contiguous"
    c = torch.empty_like(a)

    times = []
    for _ in range(10):
        start = time.perf_counter()
        softmax(a, c, num_threads=0 if parallel else 1)
        end = time.perf_counter()
        times.append(end - start)

    return np.mean(times), c.numpy()


def benchmark_triton_single(shape, a_np, axis=-1):
    return benchmark_triton(shape, a_np, axis, parallel=False)


if __name__ == "__main__":
    triton.runtime.driver.set_active_to_cpu()

    M, N = 512, 512
    x = np.random.rand(M, N).astype(np.float32)
    shape = (M, N)

    # benchmark
    time_triton_cpu, y_triton_cpu = benchmark_triton(shape, x)
    time_triton_cpu_single, y_triton_cpu_single = benchmark_triton_single(shape, x)

    assert np.allclose(
        y_triton_cpu_single, y_triton_cpu
    ), "triton_cpu single result mismatch!"
    print("pass")
