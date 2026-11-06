"""Safe evolutionary strategy expression language: no exec/eval/LLM access."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Any


OPS_BINARY = ("add", "sub", "mul", "min", "max")
OPS_UNARY = ("tanh", "abs", "neg")
ACTIONS = ("LONG", "SHORT", "FLAT", "HOLD")


def canonical(x: Any) -> bytes:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(x: Any) -> str:
    return hashlib.sha256(canonical(x)).hexdigest()


def check_expr(tree: Any, features: tuple[str,...], depth: int = 0) -> None:
    if depth > 5:
        raise ValueError("EXPRESSION_TOO_DEEP")
    if isinstance(tree, bool): raise ValueError("BAD_CONSTANT")
    if isinstance(tree, (float,int)):
        if not math.isfinite(tree) or abs(tree)>3: raise ValueError("BAD_CONSTANT")
        return
    if isinstance(tree,str):
        if tree not in features: raise ValueError("UNKNOWN_FEATURE:"+tree)
        return
    if not isinstance(tree,list) or not tree:
        raise ValueError("BAD_EXPRESSION")
    op=tree[0]
    arity = 2 if op in OPS_BINARY else 1 if op in OPS_UNARY else 0
    if not arity or len(tree)!=arity+1: raise ValueError("BAD_OPERATOR")
    for child in tree[1:]:check_expr(child,features,depth+1)


def evaluate(tree: Any, x: dict[str,float]) -> float:
    if isinstance(tree,(float,int)):return float(tree)
    if isinstance(tree,str):return x[tree]
    op=tree[0]
    a=evaluate(tree[1],x)
    if op=="tanh":y=math.tanh(a)
    elif op=="neg":y=-a
    elif op=="abs":y=abs(a)
    else:
        b=evaluate(tree[2],x)
        if op=="add":y=a+b
        elif op=="sub":y=a-b
        elif op=="mul":y=a*b
        elif op=="min":y=min(a,b)
        elif op=="max":y=max(a,b)
        else:raise ValueError("BAD_OPERATOR")
    return max(-10.0,min(10.0,y))


def random_expr(rng: random.Random, features: tuple[str,...], depth: int = 0) -> Any:
    if depth>=3 or (depth>0 and rng.random()<.51):
        return rng.choice(features) if rng.random()<.78 else round(rng.uniform(-2,2),3)
    op=rng.choice(OPS_BINARY+OPS_UNARY)
    if op in OPS_UNARY:return [op, random_expr(rng,features,depth+1)]
    return [op, random_expr(rng,features,depth+1), random_expr(rng,features,depth+1)]


@dataclass(frozen=True)
class Genome:
    scores: tuple[Any,Any,Any,Any]
    size: Any
    is_cash: bool = False

    def to_dict(self)->dict:
        if self.is_cash:return {"type":"CASH"}
        return {"type":"EXPRESSION", "scores":{k:v for k,v in zip(ACTIONS,self.scores)},"size":self.size}

    @classmethod
    def from_dict(cls,raw: dict,features:tuple[str,...])->"Genome":
        if set(raw)=={"type"} and raw["type"]=="CASH":
            return cls((0.,0.,0.,0.),0.,True)
        if set(raw)!={"type","scores","size"} or raw["type"]!="EXPRESSION":raise ValueError("BAD_GENOME_SCHEMA")
        if not isinstance(raw["scores"],dict) or set(raw["scores"])!=set(ACTIONS):raise ValueError("BAD_ACTION_SCORES")
        scores=tuple(raw["scores"][k] for k in ACTIONS)
        for expression in (*scores,raw["size"]):check_expr(expression,features)
        if len(canonical(raw))>8192:raise ValueError("GENOME_TOO_LARGE")
        return cls(scores,raw["size"])

    @property
    def sha(self)->str:
        return digest(self.to_dict())

    def action(self,x:dict[str,float])->tuple[str,float]:
        if self.is_cash:return "FLAT",0.
        vals=[evaluate(t,x) for t in self.scores]
        which=max(range(4),key=lambda i:(vals[i],-i))
        # sigmoid for smoothly learned sizing
        raw=max(-10,min(10,evaluate(self.size,x)))
        return ACTIONS[which],1/(1+math.exp(-raw))


def fresh(rng:random.Random,features:tuple[str,...])->Genome:
    return Genome(tuple(random_expr(rng,features) for _ in ACTIONS),random_expr(rng,features))


def mutate(rng:random.Random,parent:Genome,features:tuple[str,...])->Genome:
    if parent.is_cash:return fresh(rng,features)
    scores=list(copy.deepcopy(parent.scores)); size=copy.deepcopy(parent.size)
    k=rng.randrange(5)
    def sub_mutate(tree:Any)->Any:
        if rng.random()<.38 or not isinstance(tree,list):return random_expr(rng,features)
        result=copy.deepcopy(tree)
        j=rng.randrange(1,len(result))
        result[j]=sub_mutate(result[j])
        return result
    if k<4:scores[k]=sub_mutate(scores[k])
    else:size=sub_mutate(size)
    raw={"type":"EXPRESSION","scores":dict(zip(ACTIONS,scores)),"size":size}
    try:return Genome.from_dict(raw,features)
    except ValueError:return fresh(rng,features)


def cross(rng:random.Random,a:Genome,b:Genome,features:tuple[str,...])->Genome:
    if a.is_cash or b.is_cash:return mutate(rng,a if not a.is_cash else b,features)
    genes=[copy.deepcopy(rng.choice([g,h])) for g,h in zip((*a.scores,a.size),(*b.scores,b.size))]
    return Genome.from_dict({"type":"EXPRESSION","scores":dict(zip(ACTIONS,genes[:4])),"size":genes[4]},features)
