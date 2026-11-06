"""Bounded evolutionary program search; no human-made trading signal templates.

Selection uses training + internal validation. An outer test window is only
opened for the final frozen champion and benchmarks, never for candidate search.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .genome import Genome, cross, fresh, mutate
from .market import Market
from .replay import Limits, Result, simulate


@dataclass(frozen=True)
class SearchConfig:
    seed:int=77
    population:int=24
    generations:int=6
    def __post_init__(self):
        if not 0<=self.seed<=2**32-1:raise ValueError("SEED_RANGE")
        if not 6<=self.population<=128:raise ValueError("POPULATION_RANGE")
        if not 1<=self.generations<=25:raise ValueError("GENERATION_RANGE")


def splits(m:Market)->dict[str,tuple[int,int]]:
    n=len(m.times)
    if n<400:raise ValueError("NEED_AT_LEAST_400_BARS_FOR_THREE_SPLITS")
    s1=max(17,int(n*.60));s2=int(n*.80)
    if min(s1-17,s2-s1,n-s2)<60:raise ValueError("SPLIT_TOO_SHORT")
    return {"train":(17,s1),"validation":(s1,s2),"holdout":(s2,n)}


def fitness(a:Result,b:Result)->float:
    if a.ruin or b.ruin:return -9999.
    # Prioritize positive net *compound growth in both* periods.
    return min(a.log_growth,b.log_growth)-.4*max(a.max_drawdown,b.max_drawdown)-.001*max(a.turnover,b.turnover)


def search(m:Market,lim:Limits,cfg:SearchConfig,*,proposals:list[dict]|None=None)->dict:
    folds=splits(m);rng=random.Random(cfg.seed)
    cash=Genome((0.,0.,0.,0.),0.,True)
    pool=[cash]+[fresh(rng,m.active_features) for _ in range(cfg.population-1)]
    for proposed in proposals or []:
        # Strictly validated JSON AST; never execute supplied code.
        pool.append(Genome.from_dict(proposed,m.active_features))
    if len(pool)>cfg.population:pool=pool[:cfg.population]
    cache:dict[str,dict]={}
    tested=0
    def score(genome:Genome)->dict:
        nonlocal tested
        if genome.sha not in cache:
            train=simulate(m,genome,*folds["train"],lim)
            valid=simulate(m,genome,*folds["validation"],lim)
            cache[genome.sha]={"fit":fitness(train,valid),"train":train.summary(),"validation":valid.summary(),"genome":genome.to_dict()}
            tested+=1
        return cache[genome.sha]
    lineage=[]
    for generation in range(cfg.generations+1):
        ranked=sorted(pool,key=lambda g:(score(g)["fit"],g.sha),reverse=True)
        leader=ranked[0]
        lineage.append({"generation":generation,"best_sha":leader.sha,"best_fitness":round(score(leader)["fit"],7),"cash_fitness":round(score(cash)["fit"],7),"evaluated_unique":tested})
        if generation==cfg.generations:break
        new=[cash]+[g for g in ranked if not g.is_cash][:2]
        def select()->Genome:
            contenders=[rng.choice(ranked[:max(4,len(ranked)//2)]) for _ in range(3)]
            return max(contenders,key=lambda g:score(g)["fit"])
        while len(new)<cfg.population:
            if rng.random()<.28:
                candidate=fresh(rng,m.active_features)
            elif rng.random()<.25:
                candidate=cross(rng,select(),select(),m.active_features)
            else:
                candidate=mutate(rng,select(),m.active_features)
            new.append(candidate)
        pool=new
    champion=max(pool,key=lambda g:(score(g)["fit"],g.sha))
    # No search-loop access to holdout. Single evaluation after freezing champion.
    oos=simulate(m,champion,*folds["holdout"],lim,capture=True)
    cash_oos=simulate(m,cash,*folds["holdout"],lim)
    # This is only a research gate; passing it does not authorize real orders.
    status=("RESEARCH_CHALLENGER_POSITIVE" if not champion.is_cash
            and score(champion)["fit"]>0 and oos.log_growth>cash_oos.log_growth
            and not oos.ruin else "NO_PROMOTABLE_EDGE")
    return {
        "status":status,"method":"EVOLUTIONARY_AST_GENETIC_PROGRAMMING_NOT_LLM_OR_RL",
        "search": {"seed":cfg.seed,"population":cfg.population,"generations":cfg.generations,
                   "evaluated_unique":tested,"outer_holdout_reads_during_search":0},
        "folds":{k:{"first":m.times[a].isoformat(),"last":m.times[b-1].isoformat(),"bars":b-a,"bounds":[a,b]} for k,(a,b) in folds.items()},
        "lineage":lineage,"champion":champion.to_dict(),"champion_sha":champion.sha,
        "train":score(champion)["train"],"validation":score(champion)["validation"],
        "holdout":oos.summary(),"holdout_cash":cash_oos.summary(),
        "holdout_events":oos.events,"holdout_equity":oos.equity_points,
    }
