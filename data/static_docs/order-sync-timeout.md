---
id: runbook-order-sync-timeout
title: 订单同步超时排查 SOP
doc_type: runbook
service: order-sync
component: payment-api
tags:
  - timeout
  - order
  - payment-api
version: v1
source_uri: data/static_docs/order-sync-timeout.md
metadata:
  owner: sre
---

# 订单同步超时排查 SOP

## 现象

订单同步任务出现 timeout、重试次数升高或同步延迟扩大。

## 排查步骤

1. 检查 order-sync 的 p95/p99 延迟和错误率。
2. 检查 payment-api、mysql-primary、redis-cache 是否有慢调用或连接池耗尽。
3. 查看最近 30 分钟内是否有发布、配置变更或限流策略调整。
4. 如果下游 payment-api 延迟升高，先联系支付服务负责人确认容量和错误日志。

## 处理建议

优先降低重试并发，避免放大下游压力；确认下游恢复后再逐步恢复同步速率。
