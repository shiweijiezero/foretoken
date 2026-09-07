<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 持久化运行时缓存

[English](runtime-cache.md) | 简体中文

在 workload namespace 中创建一个 `RuntimeCache`，即可跨 Pod 重启保留模型和编译缓存：

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: RuntimeCache
metadata:
  name: models
  namespace: foretoken-demo
spec:
  size: 100Gi
```

将内容保存为 `runtime-cache.yaml`，并在部署模型前执行：

```bash
kubectl apply -f runtime-cache.yaml
kubectl wait --for=condition=Ready runtimecache/models \
  --namespace foretoken-demo \
  --timeout=5m
```

使用 Kustomize 时，也可以把 `runtime-cache.yaml` 加入 `resources`，由 `foretoken deploy` 和其他 manifest 一起应用。

Foretoken 默认创建 `ReadWriteMany` PVC，并使用 `Retain` 保留策略。命名空间的默认 `StorageClass` 必须支持所选访问模式。同一命名空间中的模型和 Frontend 工作负载会自动使用唯一且 Ready 的 `RuntimeCache`。

增大 `spec.size` 即可扩容 PVC，不支持缩容。

删除 `RuntimeCache` 时默认保留 PVC。设置 `spec.retentionPolicy: Delete` 后，Foretoken 会在工作负载停止使用该 PVC 后将其删除。

平台管理员也可以在安装时通过 `workload.cache.claimName` 使用已有 PVC。Foretoken 不修改或删除已有 PVC。
