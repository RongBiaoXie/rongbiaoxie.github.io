# PolarDB 的 BLOB 实现与性能优化
<p> launched in 2024.10.31, 浙江 </p>

​	**Blob (binary large object)** 是 Innodb 中的一种大对象存储类型，既可以存储字符对象，也可以存入二进制对象，在需要存储空间需求较大的数据的场景下，应用非常广泛。

​	所有可变长度的类型如 VARCHAR, VARBINARY 以及 TEXT，当记录超过页大小的一半时 (如 16 KB 的页最大记录长度为 8 KB)，也都会尝试将记录中的较长的字段使用外部 BLOB 页进行存储，直到 record 大小满足要求，为了方便，我们将这些外部存储的数据都统称为 blob 数据。

​	PolarDB 基于分布式共享存储的一写多读架构，面向在大字段写入场景做了大量性能优化，来充分利用底座分布式存储的高吞吐能力，实际测试中在优化后在高并发下能有接近 3 倍吞吐的性能提升。

​	我们先介绍 innodb 中的 blob 实现过程，前半部分主要给出 blob 的物理组织方式，后面介绍整体的并发写入机制。

### 1 Blob 的外部存储

​	在 Innodb 中，当字段长度较小时，blob 数据可以像正常字段一样直接存入主键索引的记录内。当字段长度较大时，则会单独存入主键索引结构之外的 blob page 外部页中。

​		

![](https://rongbiaoxie.github.io/images/mysql-blobs/innodb_blob.jpg)

<center>图 1:  Innodb 的索引组织 </center>

​	整个主键索引的 btree 结构因此包括两部分：一部分是存在主键索引上的小字段组成的 record，另一部分是存在外部 blob page 的大字段内容。

​	在将 blob 数据插入外部 blob page 时，会按照 blob page 的大小进行拆分，从前往后组成一个链表，我们将这部分外部管理的 Blob 区域称为 **Blob Page Chain**，其中主键索引的 record 中会留有 20 bytes，存储 blob page chain 中第一个 blob page 的位置信息，这称为 **Lob ref**。 

​	如图 2 所示，Lob ref 存有第一个 blob page 的 space_id，page_no， 数据在 page 内的偏移 offset，以及外部 blob 内容的总长度，当前 innodb 实现中， blob 长度只使用了尾部的 4 byte。另外头部的 4 byte 保留，并且借用最高的几个 bit 来存储一些控制信息，主要用于更新时，当只改变主键 record，blob 数据不变，为了减少数据拷贝开销，标记外部 blob 内容所有权的转移，这会在 purge 和 rollback 时判断是否能够清理。两个 flag 分别为

* owner flag：如果为 1，标志当前 field 不是外部 blob 的 owner，如 update 时旧 record 被标记 delete mark，而新 record 插入到主键索引的其他区域时，外部 blob page 的 owner 会从旧的 record 转移到新的 record 上，为了保证 mvcc 一致性读，从旧 record 仍能读取到外部 blob page 的内容，但是只有当前 owner 才能释放 blob page 的空间。

* Inherited flag：如果为 1，标志当前 field 的外部 blob 是从一个更老版本继承而来的，回滚时转移回旧版本，防止删除老版本的 blob 数据。

  ![](https://rongbiaoxie.github.io/images/mysql-blobs/lob_ref.jpg)

  <center>图 2:  Lob ref 的物理结构 </center>

  下面我们介绍 blob page chain 的组织方式。



### 2 Blob Page Chain

#### 2.1 MySQL 5.6/5.7 的 blob 设计

​	在 MySQL 5.6 / 5.7 的设计中，只有一种 Blob page 类型，用于存储 Blob 数据，除了头尾的元信息，都用于存储 Blob 数据。当插入一个 Blob 字段时，会将 Blob 内容按照每个 Blob page 能够容纳的最大空间进行拆分，拆成多个 page 进行存储。并将 Blob page 按照存储内容的先后顺序组成一个链表，挂在 lob ref 上。

​	在 Blob page 增加两个域，一个是 BTR_BLOB_HDR_PART_LEN 存储当前 page 的 blob 内容长度，一个是 BTR_BLOB_HDR_NEXT_PAGE_NO 存储 blob page chain 的下一个 page 位置。	

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_page_56.jpg)

<center>图 3:  MySQL 5.6/5.7 的 blob page chain 组织 </center>

​	在 5.6/5.7 版本下，无论插入和修改都是对 blob 对象的整个替换，将主键的 lob_ref 修改指向新的 blob page chain。因此每个 blob  page chain 和主键 record 是一一绑定的，可以借助 undo 日志，MVCC 读取时很容易通过构建老版本的主键 record 来查询旧的 blob page chain 的数据，如图中的老版本 lob ref 1 指向旧的 blob page chain。

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_update_57.jpg)

<center>图 4:  MySQL 5.6/5.7 的 BLOB 数据的 MVCC 一致性读 </center>

​	因为 blob 内容空间通常较大，有些情况下，只是更新其中的部分数据，而整个 blob chain 的替换，会造成空间的浪费，也引入了更大的修改开销。

#### 2.1 MySQL 8.0 的 blob 设计

​	在 MySQL 8.0 版本后，为了支持部分 blob 内容的更新，对 blob page chain 的组织进行了重新设计，增加了 lob index 的 page 类型。由于目前只在 json 处理上给出了前后 blob 内容的 diff 的偏移和长度，因此只在 json 上支持。

​	blob page chain 因此分为了 data page 和 index page 两类，以及特殊的 First page。data page 和 5.6/5.7 一样，存储 blob 的真实数据，而 index page 的全部空间都用于存储 data page 的位置索引元组 (index entry)。first page 既存储了 blob 数据也存储少量 index entry，并且 first page 也存储了将所有 index entry 串联的全局链表。主键索引的 Lob ref 指向 first page，间接地形成了 blob page chain 的链表结构。

​	![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_chain_80.jpg)

<center>图 5:  MySQL 8.0 的 blob page chain 组织 </center>

​	因为 8.0 支持部分更新，每次部分更新后，Lob ref 不再改变指向新的 Blob page chain，依然指向原来的链表。为了支持 MVCC 一致性读，8.0 在 Lob ref 中增加 Lob version （一个递增的序号，每次更新自增）的版本信息，来标识当前主键的 record 所对应需要访问的版本，在 Lob ref 上替换聊原来是多余的 BTR_EXTERN_OFFSET。全局的 Lob version 存储在 First page 的 header 中。

​	同时 8.0 提供了两种版本管理机制：

* 当修改长度小于 100 字节时（small change），全局 Lob version 不变，将修改前后完整 blob 内容的 diff 直接记录在 undo log，因此 blob page chain 的内容可以直接原地修改。当读取时，直接从 blob page chain 中获得到最新的 blob 数据后，所访问的旧版本基于 undo log 来重构。

* 当修改长度大于 100 字节时 (big change)，全局 Lob version 递增，更新时将更新区域所在的 page 复制出来，填充新的修改内容，并创建一个新的 data page 版本以及新的 index entry 指向它。Blob page chain 内部维护不同 Lob version 的 data page 和 index entry。简而言之，新的 index entry 在全局的链表中替代旧的 index entry，相同 blob 内容偏移区域的 index entry 会组成额外的 **versions 链表**，查询时通过 versions 链表选择版本。

  如图 6 (左)，新老版本的 lob ref 都指向相同的 first page。但在查询是会将 lob ref 上的所需要访问的 lob version 传入 blob page chain 的查询中，如图中旧版本主键 record 查询传入了 version 1，新版本主键 record 查询传入了 version 2。

  通过遍历 index entry 链表，选择不同版本的 data page 组装成完整的 blob 数据，返回给客户端。当在相同的 blob 数据区域有不同版本时，会从 index entry 的 versions 链表去选择对应版本的 data page，即 version 1 选择 v1 的 index entry 指向的 data page 内容。

  ![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_update_80.jpg)

  <center>图 6:  MySQL 8.0 的 BLOB 数据的 MVCC 一致性读 </center>

  具体不同类型的 blob page 结构如图 7 所示，blob page header 的属性都以 OFFSET 开头。其中每个 page 都有 OFFSET_VERSION，表明当前的 blob page 所使用格式的版本，因为当前只有一种版本，因此均为 0。

  8.0 版本的 Blob 组织关键是 **index entry** 结构，组成了完整的 blob page chain 和版本控制机制，其中

* OFFSET_PREV 和 OFFSET_NEXT： 用于串联 index entry 链表。

* OFFSET_VERSIONS：用于串联 versions 链表。

* OFFSET_TRXID：创建该 index entry 的事务 ID。

* OFFSET_TRXID_MODIFIER：修改该 index entry 的事务 ID，在 small change 时也会修改。主要用于控制 purge，清理 blob 时只有清理相同 trx id 的 undo 才会将对应的 index entry 和指向的 data page 清理了。

* OFFSET_TRX_UNDO_NO：创建该 index entry 的 undo number。

* OFFSET_TRX_UNDO_NO_MODIFIER：修改该 index entry 的 undo number，和 trxid modifier 功能一致。

* OFFSET_PAGE_NO：所指向的 data page。

* OFFSET_DATA_LEN：所指向 data page 的存储 blob 数据长度。

* OFFSET_LOB_VERSION：当前 index entry 的版本。

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_page_80.jpg)

<center>图 7:  MySQL 8.0 的 blob page 物理结构 </center>

对于 **Data page**，和 5.6/5.7 版本一样，主要存储数据部分。在头部保留当前 page 的数据部分写入长度（OFFSET_DATA_LEN），以及创建当前 page 的事务 ID (OFFSET_TRX_ID)。

对于 **Index page**，其主要内容就是存储 index entry，除了头尾信息，剩余空间都是存储 index entry，并加入到 first page 的全局链表中。

对于 **First page**，是整个 blob page chain 的入口和核心，除了保留元信息，也存储少量 index entry 和 blob 数据。

- OFFSET_FLAGS：当前 blob page chain 的控制信息，现在只有一个 bit 表示当前 blob page chain 是否支持部分更新。

- OFFSET_LOB_VERSION：当前 blob page chain 的最大 lob version，每次 big change 时递增。

- OFFSET_LAST_TRX_ID：最新修改 blob 的事务 ID。

- OFFSET_LAST_UNDO_NO：最新修改 blob 的事务 undo number。

- OFFSET_DATA_LEN：当前 page 存储 blob 数据的长度。

- OFFSET_TRX_ID：创建当前 page 的事务 ID。

- OFFSET_INDEX_LIST：整个 blob page chain 的已经分配使用的 index entry 链表。

- OFFSET_INDEX_FREE_NODES：整个 blob page chain 的空闲的 index entry 链表。


### 3 Blob 写入的并发控制

​	Blob 数据也是索引数据的一部分，在多线程并发写入修改时，需要保证索引结构的一致性。整个 blob 的并发控制流程主要在常规 btree 的并发流程\[3] 中，额外增加了 Blob page chain 的生成逻辑。

#### 3.1 Insert 路径

先以 insert 流程为例：

​	**插入主键的 record 部分**：如图 8 (左) 的流程，先遵从乐观加锁逻辑，对 index latch 加 S 锁，从 root 节点遍历到叶子结点后，X 锁住叶子结点，插入不需要外部存储的 record fields。对于需要外部存储的 record field，将数据拷贝到内存数组 big_rec_vec 中。并预留好相应的 lob ref 空间，之后，释放第一阶段的锁。

​	**生成 Blob page chain**：Blob page chain 生成前，会重新以类似悲观分裂的模式，如图 8 （右）的流程，先对 index 加 sx 锁，从 root 节点遍历到叶子结点后，X 锁住叶子结点。由于外部 Blob 数据的写入和 btree 分裂一样，都不是仅仅在原有的 page 空间上修改，需要向 tablespace 新增 page 来存储数据，申请空间需要修改 tablespace 元信息，是互斥的。

​	之后对每个 big_field，从 big_rec_vec 中取出要存储的 blob 内容，每次向 tablespace 申请一个 page，此时需要加上 tablespace 和 index segment latch 的 sx 锁（innodb 中是 root page latch），拷贝分到该 page 的 blob 数据，直到形成完整的 Blob page chain，最后修改主键索引上的 Lob ref。

​	考虑到 Blob 字段本身较大，redo 的产生量比较大，为了防止写满 redo buffer 而卡住写入情况，在写入一定量情况下，会把锁释放，提交当前已经写入的 redo，再重新从 index 开始加锁定位到主键叶子结点，接着第二阶段流程写入剩余的 blob 数据。

​	

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_insert_proc.jpg)

<center>图 8: Blob 的 insert 路径并发控制 </center>

#### 3.2 Update 路径

​	update 流程和 insert 类似，不过将 blob 操作都放在了悲观加锁的逻辑中，

​	第一阶段，如图 9 （左）的流程，在乐观加锁，对 index 加 S 锁遍历到叶子结点，准备执行主键 record 更新时，发现修改内容存在 blob 外部字段，直接释放第一阶段的锁。

​	在第二阶段悲观加锁时，如图 9 （右）的流程，对 index 加 sx 锁，从 root 节点遍历到叶子结点后，X 锁住叶子结点。根据原 record 和更新的 delta 信息先更新主键 record，对于需要外部存储的 big field，将数据拷贝到内存数组 big_rec_vec 中（包括原来不是 blob，更新后成为 blob 的内容）。

​	生成 big_rec_vec 后，对每个 big field，不支持部分更新的字段，则和 insert 路径一样，新生成一个 blob page chain。支持部分更新的字段会根据 delta 信息找到对应的 index entry，新增一个版本，最后和 insert 路径一样修改主键索引上的 Lob ref。



![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_update_proc.jpg)

<center>图 9: Blob 的 update 路径并发控制 </center>



### 4 PolarDB Blob 的写入性能优化

​	从上一节的内容我们知道，在整个 blob page chain 的生成过程都是独占 index SX 锁，此时会阻止同一个表的其他 blob 写入操作，极大地降低了 blob 的并发性能。这个瓶颈在 PolarDB 中做了优化。

​	先从 insert 路径来看，PolarDB 中将 blob page chain 的生成拆分成两部分，如图 10 的 2 和 3 部分。首先根据要存储的 blob 数据内容，从 tablespace 中批量申请出一批空闲页集合 page set，对每个大字段 big_field，从 page set 中取出一个空闲页，此时不持有索引的任何锁，离线拷贝 blob 数据。

​	最后生成所有 blob page chain 后，下一阶段通过乐观加锁逻辑，更新主键索引上的 lob_ref 指向，连接所有字段的 blob page chain。

​	在整个 blob 的写入过程中只有空间申请过程是互斥的，其他时候都允许当前表的其他 blob 并发写入操作，同时由于在 blob 数据拷贝是离线的，此时可以每次拷贝一个 blob page 后提交，不需要考虑 redo 的产生量较大，造成写满 redo buffer 而卡住的情况。



![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_polar_opt.jpg)

<center>图 10: PolarDB 的 Blob 优化 — insert 路径  </center>

​	而对于 update 路径，blob page chain 的生成过程和 insert 类似，不同的是在原先的逻辑，主键 record 的更新和 big_rec_vec 的内存数组也需要在悲观加锁逻辑下生成，在优化后，如图 11 (左)，在保留乐观加锁逻辑下，构建主键索引部分的 record 内容，然后对主键索引部分进行乐观更新，只有当主键索引部分更新造成 btree 分裂，才会进入悲观加锁逻辑。

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_polar_opt2.jpg)

<center>图 11: PolarDB 的 Blob 优化 — update 路径  </center>

​	下图对比了 16 核 cpu 在 blob 单表 100k 行长插入和更新的性能数据对比，在高并发下能有接近 3 倍的性能提升。

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_perf_data1.jpg)

<center>图 12: PolarDB 的 Blob 优化性能数据对比  </center>

### 5 Blob 的清理和回滚

​	Innodb 的数据更新删除都是标记删除，通过 UNDO 日志保存了历史数据版本，回收空间交给后台的 purge 线程异步执行，清理不会被其他事务看到的版本数据以及其对应的 UNDO 日志，对于 blob 而言，需要额外清理 blob page chain 的数据。

​	而回滚和 purge 类似，只是从最新版本回退到上一个版本，清理新生成的数据，当 blob page chain 是从老版本转移时，将 blob page chain 的所有权还给老版本的索引 record，即重置前面介绍的 lob ref 上的 owner 和 Inherited flag。

​	如下图所示，在每次 purge 时，都会以悲观加锁方式锁住整个 btree，从 undo log 中重构旧版本的主键 record。根据其中的 lob ref 进行清理 blob 数据。只有 record 版本是 blob page chain 的 owner 时，才能清理 blob page chain。

​	对于 5.6/5.7 的 blob 清理，会将整个 blob page chain 的数据清理，将所有 blob page 还给 tablespace。对于 MySQL 8.0 版本，只会根据 index entry 上的 trxid_modifier 和 undo_no_modifier 来判断是否是当前 undo 最新创建的版本，从中清理相应的 index entry 和 data page，其实这个判断也能自动确认是否删除全量数据了。

![](https://rongbiaoxie.github.io/images/mysql-blobs/blob_purge.jpg)

<center>图 13: Blob 的 purge 清理流程 </center>

### 6 引用

\[1] [MySQL · 源码分析 · innodb-BLOB演进与实现](http://mysql.taobao.org/monthly/2022/09/01/)

\[2] [MySQL · 源码分析 · BLOB字段UPDATE流程分析](http://mysql.taobao.org/monthly/2021/10/03/)

\[3] [Innodb 中的 Btree 实现 (一) · 引言 & insert 篇](http://mysql.taobao.org/monthly/2022/12/03/)
