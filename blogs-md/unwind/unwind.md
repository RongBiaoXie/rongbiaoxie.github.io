# Stack Unwind 堆栈回溯

## 1. 背景

之前做过一些关于函数堆栈的工作，但主要关注使用方法，并没有了解底层的相关实现，当和同事说起这块时，总是说不清楚，因此查阅了相关资料，这里写篇文章简单总结一下。

当系统遇到错误发生 crash 的时候，我们会通过 gdb 调试工具来查看当时的函数栈。能看到 crash 时间点的整个函数调用链，以及当时的变量值，来帮助我们快速定位问题。

我们在使用一些性能分析工具如 perf，也能看到热点函数的调用链。

<div class="language-plaintext highlighter-rouge"><div class="highlight"><pre class="highlight">
Program received signal SIGSEGV.
0x54625 in fct_b at segfault.c:5 5 printf("%l\n", *b);
(gdb) bt
0 0x54625 in fct_b at segfault.c:5
1 0x54663 in fct_a at segfault.c:10
2 0x54674 in main at segfault.c:14
(gdb) f 1
1 0x54663 in fct_a at segfault.c:10 10 fct_b((int*) a);
</pre></div></div>

**那么这些函数堆栈信息是如何获取的呢?**

在这之前，首先需要了解函数调用的底层原理，以 x86_64 架构为例 (文章[5] 对于下有非常直观详细的介绍，这里简要介绍和总结一下)。

CPU 体系结构下，是通过操作寄存器来进行计算和存储结果，当需要调用子函数时，会将当前函数的状态信息（局部变量，参数值，返回地址）保存在栈空间内，这也称为栈桢，待子函数执行完成，将结果存入相应寄存器后，再将父函数的栈桢还原。

图 1 是函数调用过程的内存结构，栈空间的寻址是通过 **RSP 寄存器** 进行控制。每次进入一个新函数，会将父函数的栈桢起始地址 (canonical frame address, CFA) 压入栈空间 (push %rbp)，然后将子函数的 CFA 存入 **RBP 寄存器** (moveq %rsp %rbp)。当结束子函数调用时，执行 leave 指令，将 RSP 寄存器指回当前函数的 CFA (moveq %rbp %rsp)，将父函数的栈桢起始位置 pop 到 RBP 寄存器，并执行 ret 指令，得到函数返回地址，RSP 寄存器的指向也回到调用前的栈空间位置。

![image](https://rongbiaoxie.github.io/images/unwind/stackframe.png)
<center>
图 1: 函数调用过程的内存结构
</center>

因此只要通过 RSP 寄存器和 RBP 寄存器的值，就能还原出当时整个函数调用栈。这也是最简单的回溯某一时刻函数调用栈的方式（gcc 编译下，-O1 往上优化需要开启 -fno-omit-frame-pointer 参数），但这种方式存在一些不足:

* 需要专门寄存器 RBP 来保存栈桢位置，并且需要额外的指令开销，即在每个函数前后增加 RBP 寄存器的出入栈和赋值开销。
* 难以还原其他寄存器的值。

为了实现仅基于 RSP 寄存器的堆栈回溯，DWARF 的调试信息出现来解决这个问题。

## 2. DWARF 

DWARF 是一种补充的调试信息，在编译时构建了一张映射表 .eh_frame，对于每个机器指令，指定当时如何计算 CFA、返回地址 (return address, ra)，以及寄存器值的内容地址，他们相对于 RSP 寄存器的偏移。

通过 readelf -wF 我们能看到可执行文件中的 .eh_frame 的最终形式，记录了映射表的格式内容，每一行对应了程序 text 段的机器指令及其 LOC 地址 (Programe Counter, PC)，行中每个实体潜在说明了当前寄存器和前一函数栈桢的在栈空间的计算规则，如当 PC=0400580 时，栈桢地址在 (rsp + 8)，return address 返回地址在栈空间地址为 (cfa - 8)，而一些 callee-save（被调用者保存）的寄存器没有入栈，所以是 undefine (u)。

<div class="language-plaintext highlighter-rouge"><div class="highlight"><pre class="highlight">
# readelf -wF test

00000088 0000000000000044 0000005c FDE cie=00000030 pc=0000000000400580..00000000004005e5
   LOC           CFA      rbx   rbp   r12   r13   r14   r15   ra
0000000000400580 rsp+8    u     u     u     u     u     u     c-8
0000000000400582 rsp+16   u     u     u     u     u     c-16  c-8
0000000000400587 rsp+24   u     u     u     u     c-24  c-16  c-8
000000000040058c rsp+32   u     u     u     c-32  c-24  c-16  c-8
0000000000400591 rsp+40   u     u     c-40  c-32  c-24  c-16  c-8
0000000000400599 rsp+48   u     c-48  c-40  c-32  c-24  c-16  c-8
00000000004005a1 rsp+56   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005ae rsp+64   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005da rsp+56   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005db rsp+48   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005dc rsp+40   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005de rsp+32   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005e0 rsp+24   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005e2 rsp+16   c-56  c-48  c-40  c-32  c-24  c-16  c-8
00000000004005e4 rsp+8    c-56  c-48  c-40  c-32  c-24  c-16  c-8
</pre></div></div>

文章[2] 有个比较直观的基于汇编流程同时计算 RBP 寄存器的例子，当程序执行过程中，不断发生变量入栈改变 RSP 时 (push、pop、sub 等)，CFA、RBP 和 RA 如何通过当时的 RSP 进行追溯，如图 2 所示，注意栈空间是从高地址向低地址扩展，因此 CFA 相对于 RSP 都是高地址。

![image](https://rongbiaoxie.github.io/images/unwind/assembly.png)
<center>
图 2: 汇编指令流程和 CFA、RBP 以及 ra 的计算，图源来自文章[2]
</center>

因此基于 DWARF 的回溯方式好处是：

*  RBP 寄存器可以当作通用寄存器使用
*  可以恢复当时所有寄存器的值
*  不需要额外在每个函数前增加入栈指令

如果都存一个上述的大表，虽然简单，但会使得程序的调试信息远远大于程序本身，因此 .en_frame 的原始信息使用更为紧凑编码格式，通过公共信息条目（CIE）和帧描述条目（FDE）指令填充的，按需解释为前面的大表，CIE 指令和 FDE 指令可以参考文章 [7]，总结来说即 CIE 是公共信息，包含多个 FDE 的桢描述信息。

通过 readelf -wf 指令能够看到可执行文件中的 .eh_frame 的编码信息：开头说明了 FDE 在.eh_frame 的 offset (00000088)、FDE 长度 (0000000000000044)、FDE 所属的 CIE (0000005c FDE cie=00000030)、以及机器指令的 PC 范围 (0000000000400580..00000000004005e5)。后面每一条存的是表格每一行和前一行的差异，由一个 FDE 指令类型 + 值组成。

<div class="language-plaintext highlighter-rouge"><div class="highlight"><pre class="highlight">
00000088 0000000000000044 0000005c FDE cie=00000030 pc=0000000000400580..00000000004005e5
  DW_CFA_advance_loc: 2 to 0000000000400582
  DW_CFA_def_cfa_offset: 16
  DW_CFA_offset: r15 (r15) at cfa-16
  DW_CFA_advance_loc: 5 to 0000000000400587
  DW_CFA_def_cfa_offset: 24
  DW_CFA_offset: r14 (r14) at cfa-24
  DW_CFA_advance_loc: 5 to 000000000040058c
  DW_CFA_def_cfa_offset: 32
  DW_CFA_offset: r13 (r13) at cfa-32
  DW_CFA_advance_loc: 5 to 0000000000400591
  DW_CFA_def_cfa_offset: 40
  DW_CFA_offset: r12 (r12) at cfa-40
  DW_CFA_advance_loc: 8 to 0000000000400599
  DW_CFA_def_cfa_offset: 48
  DW_CFA_offset: r6 (rbp) at cfa-48
  DW_CFA_advance_loc: 8 to 00000000004005a1
  DW_CFA_def_cfa_offset: 56
  DW_CFA_offset: r3 (rbx) at cfa-56
  DW_CFA_advance_loc: 13 to 00000000004005ae
  DW_CFA_def_cfa_offset: 64
  DW_CFA_advance_loc: 44 to 00000000004005da
  DW_CFA_def_cfa_offset: 56
  DW_CFA_advance_loc: 1 to 00000000004005db
  DW_CFA_def_cfa_offset: 48
  DW_CFA_advance_loc: 1 to 00000000004005dc
  DW_CFA_def_cfa_offset: 40
  DW_CFA_advance_loc: 2 to 00000000004005de
  DW_CFA_def_cfa_offset: 32
  DW_CFA_advance_loc: 2 to 00000000004005e0
  DW_CFA_def_cfa_offset: 24
  DW_CFA_advance_loc: 2 to 00000000004005e2
  DW_CFA_def_cfa_offset: 16
  DW_CFA_advance_loc: 2 to 00000000004005e4
  DW_CFA_def_cfa_offset: 8
  DW_CFA_nop
</pre></div></div>

基于这些调试信息，除了简单的计算基于 rsp 的偏移值，DWARF 还设计了灵活的 expression 表达式来实现复杂的调试信息计算，这里没有深究，mark 一下[4]。

除此之外，DWARF 还描述了源代码中的一些实体，如编译单元、函数、类型、变量等。要么直接嵌入到代码对象可执行文件的部分中，要么分割成引用的单独文件。

* .debug_line 表映射了 object code address 和源代码位置
* .debug_info 表映射了源代码变量和存储该变量的寄存器或者栈空间地址。


[1] [x86 Registers](https://wiki.osdev.org/CPU_Registers_x86-64)

[2] [Reliable and Fast DWARF-Based Stack Unwinding](https://dl.acm.org/doi/pdf/10.1145/3360572)

[3] [Allow Location Descriptions on the DWARF Expression Stack](https://llvm.org/docs/AMDGPUDwarfExtensionAllowLocationDescriptionOnTheDwarfExpressionStack/AMDGPUDwarfExtensionAllowLocationDescriptionOnTheDwarfExpressionStack.html)

[4] [通过DWARF Expression将代码隐藏在栈展开过程中](https://bbs.kanxue.com/thread-271891.htm)

[5] [x86-64 下函数调用及栈帧原理](https://zhuanlan.zhihu.com/p/27339191)

[6] [栈为什么是高地址向低地址](https://zhuanlan.zhihu.com/p/538745756)

[7] [Unwind 栈回溯详解：libunwind](https://blog.csdn.net/Rong_Toa/article/details/110846509)