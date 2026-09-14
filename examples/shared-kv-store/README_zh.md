<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 共享 KV 存储

[English](README.md) | [中文](README_zh.md)

通过一个前端运行 `Qwen/Qwen3-0.6B` 和 `Qwen/Qwen2.5-0.5B-Instruct`，两个模型共用一套 Mooncake Store。两个存储池向这套 Store 提供不同规格的容量：

| 存储池 | 实例数 | 每实例 KV 内存 | 每实例磁盘 |
|---|---:|---:|---:|
| `compact` | 1 | 1 GiB | 4 GiB |
| `large` | 1 | 4 GiB | 16 GiB |

示例共请求两张 GPU、14 核 CPU、44 GiB 内存和 21 GiB 动态存储，其中包含 Master 快照卷。模型文件和编译缓存单独保存在 `cache.yaml` 配置的 `./data` 目录中。

## 部署

使用[从源码安装的平台](../../docs/custom-deployment_zh.md)、包含 vLLM `MooncakeStoreConnector` 和 `mooncake-transfer-engine` 的 model-server 镜像，以及默认 StorageClass。模型目录的配置见[模型存储](../../docs/model-storage_zh.md)。

在仓库根目录构建 Store 镜像。使用本地 k3d 时，将镜像导入当前集群：

```bash
make image-mooncake
K3D_CLUSTER="$(kubectl config current-context)"
k3d image import foretoken-mooncake --cluster "${K3D_CLUSTER#k3d-}"
```

其他集群将 `REGISTRY` 替换为节点可访问的镜像仓库路径，执行 `make image-mooncake MOONCAKE_IMAGE=REGISTRY/mooncake` 构建，再用 `docker push REGISTRY/mooncake` 推送。将 `kvservice.yaml` 中 Master 和两个客户端的镜像都改为该地址。

```bash
foretoken deploy examples/shared-kv-store --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/shared-kv-store)"

for MODEL in Qwen/Qwen3-0.6B Qwen/Qwen2.5-0.5B-Instruct; do
  curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"What is a shared cache?\"}],\"max_tokens\":64}"
  printf '\n'
done
```

两个模型都引用同一 namespace 下的 `shared-kv`，共用存储容量；不同模型之间不复用 KV 条目。

## 调整容量和部署位置

修改 `kvservice.yaml` 中对应存储池的 `replicas`，再次执行部署命令即可。这里的副本数指存储实例数量，不是每条缓存数据的复制份数。Mooncake 负责分配和淘汰缓存；移除实例可能丢弃部分 KV，并触发重算。

Kubernetes 根据资源和卷要求选择节点。如果需要将某个池部署到已有标签对应的节点，在该池的 `name`、`replicas`、`client` 同级添加：

```yaml
nodeSelector:
  storage.example.com/class: large
```

替换为目标节点实际已有的标签。需要指定存储类型时，设置该池的 `client.disk.storageClassName`。修改池的容量规格或部署位置会替换该池实例；只修改 `replicas` 则保留未移除的实例。

查看 Store 和存储成员：

```bash
kubectl get kvservice,kvpool,kvgroup --namespace foretoken-shared-kv
```

## 清理

```bash
foretoken delete examples/shared-kv-store
```

Store 及其 PVC 会被删除，模型数据目录中的文件保留，可供下次部署复用。
