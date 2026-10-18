<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 配置 Foretoken 平台

[English](platform.md) | 简体中文

通过 `foretoken install` 配置前端访问方式、镜像来源、硬件和模型分发。默认安装和完整命令入口见 [CLI 使用指南](../../cli/README_zh.md#安装-kubernetes-平台)。

在 Foretoken 仓库根目录执行示例，并选好目标集群的 Kubernetes context。下文命令使用发布版平台；已有源码安装重新应用设置时，保留 `-e .`、镜像仓库设置和已有的 `--engine-source` 绑定。更新后的平台默认配置在重新部署服务后生效。

## 网关访问

默认通过 `LoadBalancer` 类型的 Kubernetes Service 访问模型。需要通过 Gateway 和域名统一接入时，在 `examples/quickstart/qwen3-0.6b/frontend.yaml` 的 `spec` 中添加访问域名：

```yaml
spec:
  hostname: foretoken.example.com
```

启用网关模式：

```bash
foretoken install --frontend-mode gateway
```

命令会创建专用的 `GatewayClass` 和 `Gateway`，集群没有可复用的控制器时自动安装 Envoy Gateway。随后按[网关示例](../../README_zh.md#网关模式)部署服务并发送请求。

复用已有 Gateway 时，指定它的名称和命名空间。以下以 `gateway-system` 中的 `inference-gateway` 为例，执行前替换为实际值：

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system
```

存在多个监听器时，通过 `--gateway-section-name` 指定监听器名称。使用其他控制器管理的 Gateway 时，沿用原控制器的配置和生命周期。

## 镜像与 Chart 来源

没有显式覆盖时，安装会为默认平台镜像和 Helm Chart 下载选择公共来源。需要指定来源时，通过 `--oci-registry` 传入仓库前缀，或设置 `FORETOKEN_OCI_REGISTRY`。values 中明确填写的镜像地址保持不变。

选源在运行 CLI 的机器上执行，所选仓库也需要能从集群节点访问。自动选中的代理失败时，Chart 下载和源码构建中的镜像拉取可回退到源站。

`--registry` 用于另一项任务：向远程集群分发源码构建的镜像，需要与 `-e` 一起使用。仓库配置与认证见 [Kubernetes 部署指南](kubernetes_zh.md#2-从源码安装-foretoken)。

## 自定义平台配置

需要覆盖平台设置时，把下文所需的配置写入 `deploy/platform-values.yaml`，然后应用：

```bash
foretoken install --values deploy/platform-values.yaml
```

可以重复传入 `--values` 来应用多个文件。可用设置见 [Chart values](../../deploy/charts/foretoken/values.yaml)；监控、Grafana 登录和持久日志的配置见[可观测性指南](../../observability/README_zh.md#平台设置)。

### 分配 LoadBalancer 地址

模型访问入口需要一个集群外可达的地址。k3d、K3s 和云上集群通常具备地址分配能力；通过 kubeadm、RKE2 或 Kubespray 搭建的集群可能需要另行配置 LoadBalancer。安装未发现相应能力时，会提示 `LoadBalancer support Not verified`。

需要由 Foretoken 通过 MetalLB 管理地址分配时，先向集群管理员确认一段节点网段内未被占用的 IP，再用实际地址替换下面的示例：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

### 选择硬件

安装会自动选择 NVIDIA 或沐曦运行时，并复用或安装 LeaderWorkerSet 和共享 RDMA 设备分配能力。GPU 节点仍需具备厂商驱动和 Kubernetes 设备插件。

values 中显式指定的设置优先。混合 GPU 集群通过 `runtime.vllm.gpu.resourceName` 选择 GPU 资源，或通过 `runtime.vllm.gpu.nodeSelector` 限定节点范围。沐曦环境准备见[沐曦平台指南](metax-platform_zh.md)。

### 用 Dragonfly 共享模型下载

Dragonfly 可以在节点间共享公开模型文件的下载：

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

安装命令会准备 Dragonfly，或复用已有安装。需要身份认证的模型及自定义模型源仍直接从源站下载。要选择特定的 Dragonfly Helm release，在 `modelDistribution.dragonfly` 下设置 `existingRelease: {name: dragonfly, namespace: dragonfly-system}`，替换为实际 release 名称和命名空间。

### 用 ModelExpress 传输已加载的权重

在具备 RDMA 的 NVIDIA 集群上，ModelExpress 可以从运行中的副本加载权重：

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

同时启用 Dragonfly 时，把两项设置放在同一个 `modelDistribution` 下。自动权重传输适用于使用持久缓存、数据并行度为 1、未启用专家动态负载均衡的远端模型。显式设置的 `load-format` 保持不变；没有兼容源副本时，加载已准备的文件。

修改任一分发设置后，重新应用 values，再部署需要更新的服务。将对应的 `enabled` 设为 `false` 即可关闭。`foretoken uninstall` 在没有工作负载使用时移除托管的 Dragonfly，复用的安装会保留。
