#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态查「用了但没定义的全局名」。

    python _check_names.py

★ 为什么需要: 2026-10-16 扫荡结算（1095）里写成了 `bs(...)`，而 bizserver.py 里
  序列化助手叫 `B.pb_b` —— **bizserver 里根本没有 `bs` 这个名字**。
  它在运行时抛 NameError，被 handle() 的 except 吃掉，只留一行
  `(err name 'bs' is not defined)`，**一个字节都没回给客户端** ⇒
  玩家那边表现为「扫荡结束后领奖按钮点不动」。
  自检只测 playerdata，**根本没覆盖 bizserver 的处理函数**，所以没抓到。

  这个脚本用 AST 把每个模块里的「全局名读取」列出来, 减去
  内置名 / import 进来的名字 / 模块级 def-class-赋值 / 函数参数和局部变量,
  剩下的就是可疑的未定义全局名。
"""
import ast
import builtins
import os
import sys

sys.stdout.reconfigure(errors="replace")

# 仓库根 = tools/ 的上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [os.path.join(ROOT, f)
         for f in ("bizserver.py", "playerdata.py", "fakeserver.py")]

BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__builtins__",
}


def module_globals(tree, src):
    """模块级定义的名字：import / def / class / 赋值 / for / with / try 里的绑定。"""
    names = set()

    def add_target(t):
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)

    # ★ 顶层的 if __name__ == "__main__": / try: 里也会有定义，要一起收
    top = []
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        top.append(node)
        for field in ("body", "orelse", "finalbody"):
            stack.extend(getattr(node, field, []) or [])
        for h in getattr(node, "handlers", []) or []:
            stack.extend(h.body)

    for node in top:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                add_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            add_target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add_target(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    add_target(item.optional_vars)
        elif isinstance(node, ast.Try):
            for h in node.handlers:
                if h.name:
                    names.add(h.name)
        elif isinstance(node, (ast.If, ast.While, ast.With, ast.For,
                               ast.AsyncFor, ast.AsyncWith, ast.AsyncFunctionDef)):
            pass            # 里面已经由上面的展开收过了
    return names


def local_scopes(tree):
    """所有函数里绑定过的名字（参数、赋值、for、with、except、嵌套 def）。"""
    names = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(fn, ast.Lambda):
            args = fn.args
        else:
            args = fn.args
            names.add(fn.name)
        for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
            names.add(a.arg)
        if args.vararg:
            names.add(args.vararg.arg)
        if args.kwarg:
            names.add(args.kwarg.arg)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = n.body
            for st in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(st, ast.Name) and isinstance(st.ctx, ast.Store):
                    names.add(st.id)
                elif isinstance(st, (ast.Import, ast.ImportFrom)):
                    for a in st.names:
                        names.add((a.asname or a.name).split(".")[0])
                elif isinstance(st, ast.ExceptHandler) and st.name:
                    names.add(st.name)
    # ★ 推导式/生成器自己的循环变量（Python 3 里作用域独立）—— 不加会误报
    for n in ast.walk(tree):
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                          ast.GeneratorExp)):
            for gen in n.generators:
                for x in ast.walk(gen.target):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
        if isinstance(n, ast.comprehension):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    names.add(x.id)
    return names


bad_total = 0
for path in FILES:
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        continue
    tree = ast.parse(src, path)
    defined = module_globals(tree, src) | local_scopes(tree) | BUILTINS
    used = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            used.setdefault(n.id, n.lineno)
    bad = sorted((ln, nm) for nm, ln in used.items() if nm not in defined)
    print("=== %s" % path.rsplit("\\", 1)[-1])
    if not bad:
        print("   OK 没有未定义的全局名")
    for ln, nm in bad:
        print("   !! 第 %d 行用了未定义的名字 `%s`" % (ln, nm))
    bad_total += len(bad)

print()
print("可疑名字合计 %d 个" % bad_total)
sys.exit(1 if bad_total else 0)
