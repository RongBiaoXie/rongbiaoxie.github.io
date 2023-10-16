# MySQL 的 SQL 查询执行流程
<p> launched in 2023.10.15, 浙江 </p>

[前一篇内容](https://zhuanlan.zhihu.com/p/661633639)结合了编译器了解了 SQL engine 的一些知识。

这里具体介绍 Expression 在 MySQL 是如何编译执行的，即 query 的衡量，评估，执行是如何实现的。

首先简单介绍一下 MySQL 中的 Expression 的含义是什么？

### MySQL 中的表达式

​	在 MySQL 中，表达式 （expression）是一系列算子，数值，函数的集合。一般可以在一个查询语句的三个位置找到：

- 在 SELECT 语句的 projection list，SELECT 真正查询的 column 列表
- WHERE 语句的 selection list，where 中的用来过滤的 column 列表
- HAVING 语句的 aggregated selection。Having 中的聚集列表

2 和 3 主要是 filter expression，通常由算数，比较算子构成。

除此之外，还有 Join 条件，group 条件，order by 条件，limit 语句中也会包含 expression。

![](https://rongbiaoxie.github.io/images/sqlengine/expression_list.png)

​	在 MySQL 中，这些 expression 在代码中是以树形的**继承类 Item** 表示。Item类很重要。是整个语句操作对象的基类，用于表示 expression 的不同组件，例如逻辑操作符、算术操作符、常量值、字段引用等等都是一个 item。例如整数使用 Item_int 表示，表示 SQL 中某个整数的常量值。相等运算符（=）使用 Item_func_eq 表示，可以计算其他两个 Item 的相等性。

​	根据这些类的性质，**可以以 Item 的树形式构造所有类型的表达式**，其中根节点的 Item 表示完整的表达式， 叶节点是涉及到的算子。如下图用 Item 表示了表达式 “age = 26 AND name = ’Peter’”。

![](https://rongbiaoxie.github.io/images/sqlengine/item_tree.png)

​	为了衡量和执行 Item tree 表示的 expression，每个 Item 类实现了一系列的虚函数来计算 Item 的值。最值得注意的是 val_int() 方法，返回被衡量 Item 的 64 位 int 值。事实上，对于最基本的类型，都存在一组val_\<TYPE\>方法，它们计算给定类型 expression 的求值。

​	例如，Item_func_eq 的 val_int() 方法计算树中另外两个子 item 的相等性， 其中要比较的算子是通过调用子 item 的 val_int () 方法提取的。

​	MySQL 对于支持的数据类型 和 SQL 函数，都有一个 Item 派生类。从大量的 Item 派生类中，有几个派生类是比较重要的：

- Item_int:：表示 expression 中的常量整数。
- Item_string：表示 expression 中的常量字符串。
- Item_field：表示 table 中的一个 column，对于给定的行，val_\<TYPE\>() 函数返回 column 类型对应的值。
- Item_func_eq：表示 = 算子
- Item_func_gt：表示 > 算子
- Item_cond_and：表示逻辑运算 AND 算子
- Item_cond_or：表示逻辑运算 OR 算子

### MySQL 中 Query 的生命周期

为了了解 expression 在 MySQL 是怎么执行的，主要是看 query 在 MySQL 中的生命周期：parsing，preparing，optimizing 和 execution。下图是 MySQL 查询引擎的生命周期，每个组件的输入和输出，到最后怎么拿到真实的存储引擎的数据：数据流动过程是从 SQL statement 到 Query_block 到 AccessPath，到 Iterators，再到存储引擎。

![](https://rongbiaoxie.github.io/images/sqlengine/mysql_query_engine.png)

### parser

当数据库从客户端接收 SQL 查询并将查询发送给 parser 时，查询的生命周期就开始了。首先是 lexical scanner (词法扫描)，将整个 SQL 语句解析成 token 流，如 “SELECT count(*), state FROM customer GROUP BY state” 解析成了一个个 token 

```sql
• SELECT
• count
• (
• *
• )
• ,
• state
• FROM
• customer
• GROUP
• BY
• state
```

MySQL 在编译前就生成了每个 **token 和 parse_tree 中数据结构类型的哈希映射**，通过语法检查后将查询的文本转换为结构化的 item 树格式，来表示 SQL 查询的不同部分。

**Parse tree:** 

在 MySQL 中，parse tree 是一个C++ 结构体，这里也称为 **Query_block**。MySQL 代码中表示为 SELECT_LEX 类，其中 SELECT_LEX_UNIT 是包含一系列的 SELECT_LEX 如 UNION 中的子查询，根据查询的结构形成递归包含关系。

SELECT_LEX 包含了一个查询重要的信息：

- table_list, 
- field list, 
- where 条件的 item 子树，优化器大部分需要的信息都来自于这个 item 子树。最后优化器是会根据 item 计算构建过滤器来过滤 record
- having 子树，
- 以及本身 select expression 的 item list. 

即给定查询的 Items 是在整个执行过程的最开始构造的，对于 WHERE、SELECT 和 HAVING 子句中的表达式，parser 会构造等价的 **Item 树**，直接存储在Query_block 中。

当 parser 完成时，输出是一个完整的 Query_block。

### Prepare

这个阶段主要是 解析 (resolve) 和转化 (transform)。

在这之前，**Query_block 没有和 table 以及 column 的存储位置相关联**。这个关系是在解析阶段（setup_fields）建立的。例如，Item_field 实例被初始化为对应表的第一行，指向第一个列的值的存储位置。此外，这也是**应用类型检查**的地方。

Prepare 的另一个重要工作是简化 Query_block 的内容。优化 Item 树来减少计算深度。如前面提到的常量折叠（直接给出常量计算值）和无效代码的去除。在解析和转换 Query_block 的不同部分之后，如下图就是将 “20>10” 和 “100+40” 两个直接运算简化的过程，该阶段的最终输出简化的 Query_block 称为**逻辑查询计划**。

并且会执行 setup_table，得到 optimizer 优化的 table reference，优化器最直接优化和依赖的就是 prepare 给出的 Query_block 和需要优化的 table 列表。

在 mysql 代码中，不同 DML 类型的 Query_block 对应于不同 Sql_cmd_dml 类型的 prepare 函数，继续调用到 SELECT_LEX_UNIT::prepare() 。

![](https://rongbiaoxie.github.io/images/sqlengine/prepare_transform.png)

### Optimizer

​	优化器目的是得到物理执行计划，在 mysql 8.0.22 之前，是通过 **JOIN** 和**QEP_TAB** (Query Execution Plan Table) 两个结构来表示执行计划。8.0.22 之后，采用了 **AccessPath** 的结构来表示。

​	对于优化器来说，所有的查询都是 join，每个 Query_block 在处理的时候，都被当作 JOIN 对象来处理，**JOIN::optimize()** 是所有 Query_block 优化的入口函数，目的是将 Query_block 转化成 QEP_TAB 或者 AccessPath。其主要工作:

- **逻辑转化**：outer join 转为 inner join，分区裁剪，ORDER BY 优化等等。

- **make_join_plan**：基于 cost 估计，

  - 对每个表计算最优的 join 顺序 (choose_table_order，有三种方式，straight 用 hint 指定，find_best 穷举，以及 greedy search按一定深度贪心选择)，Join table 由一个描述左深树的数组 (**JOIN_TAB**) 表示。
  - 以及每个表的访问方式如 single key read, range scan, table scan, index scan（best_access_path，将选取的 index，table， cost 存入 Position 结构中）。

  为了得到所有可能的 access path 来确定访问表的方式，优化器会先调用 update_ref_and_keys，**根据 where 的 item 子树建立每个 table 可能会用到的索引和 key**，方便优化器寻找访问表的方案。

  最后最优方案通过 get_best_combination 构建，保存在 best_ref 中，并确定每个 JOIN_TAB 表访问的 type。

- **alloc_qep**：这里主要生成物理执行计划 QEP_TAB，JOIN_TAB 和 QEP_TAB 有相同基类，主要将 POSITION 等信息赋值过去。

- **make_join_readinfo**：细化执行计划，对每张表，基于优化器选择的访问类型，建立构建 iterators 需要的一些信息。

- **create_iterators**:  构建火山引擎执行器所需要的 RowIterators。

  - 首先基于执行计划给出的访问方式（pick_table_access_method）构建 QEP_TAB 中所有 table 的 RowIterators 来访问每个表的数据（**create_table_iterators**）。
  - 接着构建 create_root_iterator_for_join 构建出整个 Query_block 的 root iterator 以及 iterator 树，其中最重要的是 **ConnectJoins** 函数，基于不同的 Join 类型（netloop/hashjoin/semi-join/antjoin），将 前面 JOIN 每个表构建的 iterator 组合起来，构建成火山引擎执行器执行的 iterator tree。

  8.0.22 之后，是先构建 Access Path，再构建一一对应的 RowIterator。 

### Execution

​	MySQL 8.0 之后采用了 火山引擎执行器，通过优化器最后构建的 RowIterators，从 root RowIterator 链式调用 Read() 接口，当需要的数据由其他 RowIterator 提供时，调用下一个 RowIterator 的 Read()，Read 每次读取一行，直到返回 EOF。例如 SortingIterator 是将其他 RowIterator 读取的数据，进行排序返回。

​	执行器核心函数 **ExecuteIteratorQuery**，调用 Query_expression 的 root_iterator Read()，再到每个 Query_block 的 root_iterator Read()，再基于优化器给出的 join 顺序一层层提取表中数据。

```c++
ExecuteIteratorQuery:
m_root_iterator->Init()
  	while m_root_iterator->Read() is not empty: // 循环读每一行
		query_result->send_data(thd, *fields)	// 发送当前行数据给 client
```

​	Query_block 中不同表的数据流动是由 Join iterator 连接，常用的 netloop join（嵌套循环）iterator 逻辑，不同 join 方式有很多资料可以查询，这里主要介绍下 mysql 最常用的 netloop，主要是先读外表 iterator 数据，再 check 内表 iterator 数据，直到所有数据都读上来。

```c++
while true:
	if state is NEEDS_OUTER_ROW // 需要外表数据
		m_source_outer->Read() // 读一行外表数据
      	state = READING_INNER_ROWS  // 需要内表数据 check
	
    err = m_source_inner->Read() // 基于当前外表数据读取和 check 内表
    if err = -1: // 基于当前外表数据读取内表为空
		state = NEEDS_OUTER_ROW   // 读取新的外表数据来 check
```

​	每个 RowIterator 读数据时，还会 check 一些 join，having 条件，这个由 FilterIterator 来控制。将实际读取行的 iterator 得到的数据，通过 item 树形式的条件值（m_condition->val_int）来判断读取的行是否需要。

​	而真正和存储引擎读数据的是如 EQRefIterator，IndexRangeScanIterator 这种和 table 访问方式相关的 iterator 来控制的，调用 存储引擎 handler 提供的各种访问接口来读取数据。

[1] Compiling expressions in MySQL, Anders Hallem Iversen.

[2] Understanding MySQL internals.

\[3\] [ MySQL · 内核特性 · 8.0 新的火山模型执行器](http://mysql.taobao.org/monthly/2020/07/01/).

\[4\] [MySQL 8.0 Server 层最新架构详解](https://zhuanlan.zhihu.com/p/394048272)

\[5\] [MySQL源码解析之执行计划](https://www.cnblogs.com/greatsql/p/16560603.html)

\[6\] [庖丁解牛-MySQL查询优化器之JOIN ORDER](https://zhuanlan.zhihu.com/p/542499821) 

\[7\] [MySQL · 源码分析 · Derived table代码分析](https://zhuanlan.zhihu.com/p/443656156)

\[8\] [MySQL · 内核特性 · semi-join四个执行strategy