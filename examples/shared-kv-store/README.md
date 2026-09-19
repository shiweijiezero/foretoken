<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Shared KV Store

[English](README.md) | [中文](README_zh.md)

Serve `Qwen/Qwen3-0.6B` and `Qwen/Qwen2.5-0.5B-Instruct` through one frontend and one Mooncake Store. Two storage pools contribute different capacities to the same Store:

| Pool | Instances | KV memory per instance | Disk per instance |
|---|---:|---:|---:|
| `compact` | 1 | 1 GiB | 4 GiB |
| `large` | 1 | 4 GiB | 16 GiB |

The example requests two GPUs, 14 CPU cores, 44 GiB memory, and 21 GiB of dynamically provisioned storage, including the Master snapshot. Model files and compilation caches use the separate `./data` directory in `cache.yaml`.

## Deploy

Use a [source-installed platform](../../docs/custom-deployment.md), a model-server image containing vLLM's `MooncakeStoreConnector` and `mooncake-transfer-engine`, and a default StorageClass. Configure the model directory as described in [Model storage](../../docs/model-storage.md).

Build the Store image from the repository root. For a local k3d cluster, import it into the active cluster:

```bash
make image-mooncake
K3D_CLUSTER="$(kubectl config current-context)"
k3d image import foretoken-mooncake --cluster "${K3D_CLUSTER#k3d-}"
```

For another cluster, replace `REGISTRY` with a registry repository accessible to its nodes. Build with `make image-mooncake MOONCAKE_IMAGE=REGISTRY/mooncake`, push the image with `docker push REGISTRY/mooncake`, and use that image in the Master and both clients in `kvservice.yaml`.

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

Both models reference `shared-kv` in the same namespace. They share storage capacity, not KV entries between different models.

## Adjust capacity and placement

Edit each pool's `replicas` in `kvservice.yaml` and run the same deploy command. Replicas count storage instances, not copies of each cached value. Mooncake manages placement and eviction; removing an instance can discard cached KV and cause recomputation.

For a memory-only pool, omit `client.disk`. Its `memoryCapacity` contributes host memory to the same shared Store without a client PVC or SSD offload. Memory-only and disk-backed pools can coexist; Master snapshot storage is configured separately.

Kubernetes chooses nodes from resource and volume requirements. To restrict a pool to an existing node label, add `nodeSelector` beside its `name`, `replicas`, and `client` fields:

```yaml
nodeSelector:
  storage.example.com/class: large
```

Use a label present on the intended nodes. Set that pool's `client.disk.storageClassName` when selecting a particular storage class. Changing pool capacity or placement settings replaces its instances; changing only `replicas` preserves the retained instances.

Inspect the Store and its members:

```bash
kubectl get kvservice,kvpool,kvgroup --namespace foretoken-shared-kv
```

## Clean up

```bash
foretoken delete examples/shared-kv-store
```

The Store and its PVCs are deleted. Files in the model data directory remain available for redeployment.
