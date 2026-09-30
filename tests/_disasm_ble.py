# -*- coding: utf-8 -*-
"""Disassemble EgoLowBle_Create (and a couple others) to recover the real ABI."""
import struct
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

DLL = r"D:\Soft&tools\软件工具\EGOViewer_v2.0.10_202609061205_ad4a28e_win_x64\EgoLowBle.dll"


def load_pe(path):
    with open(path, "rb") as f:
        data = f.read()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e_lfanew + 4
    nsec = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
        exp_rva, exp_size = struct.unpack_from("<II", data, opt + 112)
    else:
        image_base = struct.unpack_from("<I", data, opt + 28)[0]
        exp_rva, exp_size = struct.unpack_from("<II", data, opt + 96)
    sh = opt + opt_size
    secs = []
    for i in range(nsec):
        off = sh + i * 40
        name = data[off:off+8].rstrip(b"\x00").decode("latin1")
        vsize, vaddr, rsize, roff = struct.unpack_from("<IIII", data, off + 8)
        secs.append((name, vaddr, vsize, roff, rsize))

    def rva_to_off(rva):
        for name, vaddr, vsize, roff, rsize in secs:
            if vaddr <= rva < vaddr + max(vsize, rsize):
                return roff + (rva - vaddr)
        return None

    # exports
    exports = {}  # name -> rva
    eo = rva_to_off(exp_rva)
    n_names = struct.unpack_from("<I", data, eo + 24)[0]
    n_funcs = struct.unpack_from("<I", data, eo + 20)[0]
    addr_rva = struct.unpack_from("<I", data, eo + 28)[0]
    name_rva = struct.unpack_from("<I", data, eo + 32)[0]
    ordinal_rva = struct.unpack_from("<I", data, eo + 36)[0]
    ao = rva_to_off(addr_rva)
    noff = rva_to_off(name_rva)
    oo = rva_to_off(ordinal_rva)
    addrs = [struct.unpack_from("<I", data, ao + i*4)[0] for i in range(n_funcs)]
    for i in range(n_names):
        nrva = struct.unpack_from("<I", data, noff + i*4)[0]
        noff2 = rva_to_off(nrva)
        end = data.index(b"\x00", noff2)
        name = data[noff2:end].decode("latin1")
        ordinal = struct.unpack_from("<H", data, oo + i*2)[0] - 1
        exports[name] = addrs[ordinal]
    return data, secs, exports, image_base


data, secs, exports, image_base = load_pe(DLL)


def rva_to_off(rva):
    for name, vaddr, vsize, roff, rsize in secs:
        if vaddr <= rva < vaddr + max(vsize, rsize):
            return roff + (rva - vaddr)
    return None


def disasm(func_name, max_ins=90):
    rva = exports.get(func_name)
    if rva is None:
        print(func_name, ": not exported")
        return
    off = rva_to_off(rva)
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    code = data[off:off+20]
    insn = next(md.disasm(code, image_base + rva))
    # follow one unconditional jmp (thunk) to the real body
    if insn.mnemonic == "jmp":
        target = int(insn.op_str, 16)
        rva2 = target - image_base
        off2 = rva_to_off(rva2)
        print(f"{func_name}: export RVA 0x{rva:X} -> jmp {insn.op_str} (real body RVA 0x{rva2:X})")
        rva, off = rva2, off2
    else:
        print(f"{func_name}: export RVA 0x{rva:X} (no leading jmp)")
    print(f"===== {func_name}  (RVA 0x{rva:X}) =====")
    code = data[off:off+max_ins*16]
    for i, ins in enumerate(md.disasm(code, image_base + rva)):
        print(f"  0x{ins.address:08X}: {ins.mnemonic}\t{ins.op_str}")
        if i > max_ins:
            break


for f in ["EgoLowBle_Create", "EgoLowBle_Destroy", "EgoLowBle_GetLastError"]:
    disasm(f)
