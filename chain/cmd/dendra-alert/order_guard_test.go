package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"runtime"
	"testing"
)

// The order assertion also checks the ordering step, since map iteration may
// happen to be sorted even when that step is missing.
func requireFiringKeysSorted(t *testing.T) {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate the ordering source")
	}
	f, err := parser.ParseFile(token.NewFileSet(), filepath.Join(filepath.Dir(file), "main.go"), nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var reconcile *ast.FuncDecl
	for _, d := range f.Decls {
		if fn, ok := d.(*ast.FuncDecl); ok && fn.Recv != nil && fn.Name.Name == "reconcile" {
			reconcile = fn
			break
		}
	}
	if reconcile == nil {
		t.Fatal("reconcile is missing")
	}
	matched := false
	ast.Inspect(reconcile.Body, func(n ast.Node) bool {
		block, ok := n.(*ast.BlockStmt)
		if !ok {
			return true
		}
		for i := 0; i+2 < len(block.List); i++ {
			collect, ok := block.List[i].(*ast.RangeStmt)
			if !ok {
				continue
			}
			source, ok := collect.X.(*ast.Ident)
			if !ok || source.Name != "firing" {
				continue
			}
			expr, ok := block.List[i+1].(*ast.ExprStmt)
			if !ok {
				continue
			}
			call, ok := expr.X.(*ast.CallExpr)
			if !ok || len(call.Args) != 1 {
				continue
			}
			selector, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || selector.Sel.Name != "Strings" {
				continue
			}
			pkg, ok := selector.X.(*ast.Ident)
			if !ok || pkg.Name != "sort" {
				continue
			}
			arg, ok := call.Args[0].(*ast.Ident)
			if !ok || arg.Name != "keys" {
				continue
			}
			send, ok := block.List[i+2].(*ast.RangeStmt)
			if !ok {
				continue
			}
			ordered, ok := send.X.(*ast.Ident)
			if !ok || ordered.Name != "keys" {
				continue
			}
			matched = true
		}
		return true
	})
	if !matched {
		t.Fatal("firing keys must be sorted between collection and delivery")
	}
}
