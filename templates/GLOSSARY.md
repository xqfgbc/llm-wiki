# 术语表

> **唯一译法**。编译 wiki 页时必须使用「中文」列；英文原文首次出现时用括号括注。
> 本表同时用于**查询扩展**：中文提问会自动补上对应的英文术语，
> 解决「中文提问 × 英文语料」的跨语言召回问题。

## 概念

| 英文 | 中文 | 禁用译法 |
|---|---|---|
| Credential Brokering | 凭证代理 | 凭证经纪、经纪人 |
| Credential Broker | 凭证代理器 | 经纪人 |
| Token Vault | 令牌库 | — |
| Confused Deputy | 混淆代理问题 | 困惑的副手 |
| Token Passthrough | 令牌透传 | 令牌直通 |
| Token Exchange | 令牌交换 | 令牌兑换 |
| Scope Minimization | 权限最小化 | 作用域最小化 |
| Defense in Depth | 纵深防御 | 深度防御 |
| Prompt Injection | 提示注入 | 提示词注入 |
| Memory Poisoning | 记忆投毒 | 记忆污染 |
| Tool Misuse | 工具滥用 | — |
| Goal Hijacking | 目标劫持 | 目标劫持攻击 |
| Human-in-the-Loop | HITL | 人在回路、人工介入环节 |
| Least Privilege | 最小权限 | 最小特权 |
| Egress Control | 出口管控 | 出站控制 |
| Audit Trail | 审计追踪 | 审计线索 |
| Envelope Encryption | 信封加密 | 快速信封加密 |
| KMS Grant | KMS 授权 | KMS 资助 |
| Security Token Service | 安全令牌服务 | — |
| On-Behalf-Of | 代理授权 | 代表流 |
| Supply Chain | 供应链 | — |
| Sandbox | 沙箱 | 沙盒 |
| Autonomy | 自主性 | 自治度 |
| Observability | 可观测性 | 可观察性 |
| Blast Radius | 影响半径 | 爆炸半径 |
| Zero Trust | 零信任 | — |
| Threat Model | 威胁模型 | — |
| Attack Surface | 攻击面 | — |
| Runtime Governance | 运行时治理 | — |
| Identity Assertion | 身份断言 | — |

## 专有名词

> 可指称的具体对象：产品、服务、组件、标准、协议、资源。抽取 pipeline 用它做类型提示与别名归并。

| 名称 | 类型 | 中文/说明 | 别名 |
|---|---|---|---|
| Istio | product | 服务网格 | Istio Service Mesh |
| Istio Ambient | mode | Istio 的 ambient 数据面模式 | Ambient 模式 |
| Istio Sidecar | mode | Istio 的 sidecar 数据面模式 | Sidecar 模式 |
| Kubernetes | product | 容器编排平台 | K8s |
| Amazon EKS | service | AWS 托管 Kubernetes | EKS |
| VPC | service | AWS 虚拟私有云 | Virtual Private Cloud |
| NAT Gateway | service | AWS NAT 网关 | NAT 网关 |
| ALB | service | AWS 应用负载均衡器 | Application Load Balancer |
| NLB | service | AWS 网络负载均衡器 | Network Load Balancer |
| VPC CNI | component | EKS 的 VPC CNI 插件 | Amazon VPC CNI, aws-node |
| IRSA | feature | IAM Roles for Service Accounts | — |
| ECR | service | 弹性容器镜像仓库 | Elastic Container Registry |
| CloudWatch | service | AWS 监控服务 | Amazon CloudWatch |
| CoreDNS | component | Kubernetes DNS | — |
| kube-proxy | component | Kubernetes 服务代理 | — |
| Gateway API | standard | Kubernetes 网关标准 | — |
| ztunnel | component | Ambient 的 L4 代理 | — |
| waypoint | component | Ambient 的 L7 代理 | — |
| HBONE | protocol | HTTP-Based Overlay Network Environment | — |
| SPIFFE | standard | 安全工作负载身份标准 | Secure Production Identity Framework for Everyone |
| mTLS | protocol | 双向 TLS | mutual TLS |
| NetworkPolicy | resource | Kubernetes 网络策略 | — |
| ServiceAccount | resource | Kubernetes 服务账号 | — |
| ClusterIP | service type | 集群内部虚拟 IP | — |
| NodePort | service type | 节点端口暴露 | — |
| LoadBalancer | service type | 负载均衡器暴露 | LB |
| Kustomization | resource | Flux 的 Kustomize 资源 | — |
| Flux CD | product | GitOps 持续交付工具 | Flux |
| AWS KMS | service | AWS 密钥管理服务 | KMS, Key Management Service |
| OpenBao | product | 开源密钥管理 | — |
| agentgateway | product | Agent 网关 | — |
| MCP | protocol | Model Context Protocol | Model Context Protocol |
| MCP Gateway | component | MCP 网关 | — |
| CB4A | standard | Credential Brokering for AI Agents | — |
| OAuth | standard | 开放授权协议 | OAuth 2.0 |
| JWT | standard | JSON Web Token | — |
| STS | service | AWS Security Token Service | Security Token Service |
| IdP | component | 身份提供方 | Identity Provider |
| OWASP | org | 开放 Web 应用安全项目 | — |
| NIST AI RMF | standard | NIST AI 风险管理框架 | NIST |
| IAM | service | AWS 身份与访问管理 | Identity and Access Management |
| Amazon EC2 | service | AWS 弹性计算 | EC2 |
| Amazon S3 | service | AWS 对象存储 | S3 |
| AWS Certificate Manager | service | AWS 证书管理 | ACM |
| Spring Cloud Gateway | product | Spring 网关 | SCG |
| xDS | protocol | xDS 控制面协议 | — |
| CRI | interface | 容器运行时接口 | Container Runtime Interface |
| AWS | org | Amazon Web Services | Amazon Web Services |
| SVID | standard | SPIFFE Verifiable Identity Document | SPIFFE Verifiable Identity Document |
| JWKS | standard | JSON Web Key Set | JSON Web Key Set |
| CEL | language | Common Expression Language | Common Expression Language |
| PDP | component | Policy Decision Point | Policy Decision Point |
| HMAC | standard | Hash-based Message Authentication Code | Hash-based Message Authentication Code |
| OWASP LLM01 | standard | OWASP LLM01 提示注入风险 | LLM01 |

## 缩写

> 缩写必须在这里登记全称。抽取阶段遇到未登记的全大写词会进入对抗复核，不会被静默当成实体。

| 缩写 | 全称 | 中文 | 说明 |
|---|---|---|---|
| VPC | Virtual Private Cloud | 虚拟私有云 | — |
| NAT | Network Address Translation | 网络地址转换 | 与 NAT Gateway 区分 |
| ALB | Application Load Balancer | 应用负载均衡器 | — |
| NLB | Network Load Balancer | 网络负载均衡器 | — |
| EKS | Elastic Kubernetes Service | 弹性 Kubernetes 服务 | — |
| ECR | Elastic Container Registry | 弹性容器镜像仓库 | — |
| IRSA | IAM Roles for Service Accounts | 服务账号 IAM 角色 | — |
| CNI | Container Network Interface | 容器网络接口 | — |
| ENI | Elastic Network Interface | 弹性网卡 | — |
| IAM | Identity and Access Management | 身份与访问管理 | — |
| IdP | Identity Provider | 身份提供方 | — |
| JWT | JSON Web Token | JSON Web 令牌 | — |
| STS | Security Token Service | 安全令牌服务 | 注意与 AWS STS 区分 |
| OIDC | OpenID Connect | OpenID 连接协议 | — |
| CRD | Custom Resource Definition | 自定义资源定义 | — |
| IPVS | IP Virtual Server | IP 虚拟服务器 | — |
| SNAT | Source Network Address Translation | 源地址转换 | — |
| DNAT | Destination Network Address Translation | 目的地址转换 | — |
| CIDR | Classless Inter-Domain Routing | 无类别域间路由 | — |
| RAG | Retrieval-Augmented Generation | 检索增强生成 | — |
| DEK | Data Encryption Key | 数据加密密钥 | — |
| KEK | Key Encryption Key | 密钥加密密钥 | — |
| HBONE | HTTP-Based Overlay Network Environment | 基于 HTTP 的覆盖网络环境 | — |
| SPIFFE | Secure Production Identity Framework for Everyone | 安全生产身份框架 | — |
| mTLS | mutual Transport Layer Security | 双向 TLS | — |
| L4 | Layer 4 | 传输层 | — |
| L7 | Layer 7 | 应用层 | — |

## 通用缩写（不建页）

> 这些词需要展开和检索，但不是具体的可指称对象。抽取阶段只用来消解缩写，不生成页面候选。

| 缩写 | 全称 | 中文 | 说明 |
|---|---|---|---|
| API | Application Programming Interface | 应用程序接口 | 通用缩写，不建页 |
| AI | Artificial Intelligence | 人工智能 | 通用缩写，不建页 |
| LLM | Large Language Model | 大语言模型 | 通用缩写，不建页 |
| HTTP | Hypertext Transfer Protocol | 超文本传输协议 | 通用缩写，不建页 |
| HTTPS | HTTP Secure | 安全超文本传输协议 | 通用缩写，不建页 |
| TCP | Transmission Control Protocol | 传输控制协议 | 通用缩写，不建页 |
| UDP | User Datagram Protocol | 用户数据报协议 | 通用缩写，不建页 |
| IP | Internet Protocol | 互联网协议 | 通用缩写，不建页 |
| DNS | Domain Name System | 域名系统 | 通用缩写，不建页 |
| TLS | Transport Layer Security | 传输层安全 | 通用缩写，不建页 |
| JSON | JavaScript Object Notation | JSON 数据格式 | 通用缩写，不建页 |
| YAML | YAML Ain't Markup Language | YAML 数据格式 | 通用缩写，不建页 |
| SQL | Structured Query Language | 结构化查询语言 | 通用缩写，不建页 |
| SDK | Software Development Kit | 软件开发工具包 | 通用缩写，不建页 |
| CLI | Command Line Interface | 命令行接口 | 通用缩写，不建页 |
| URL | Uniform Resource Locator | 统一资源定位符 | 通用缩写，不建页 |
| CPU | Central Processing Unit | 中央处理器 | 通用缩写，不建页 |
| RAM | Random Access Memory | 随机存取存储器 | 通用缩写，不建页 |
| DB | Database | 数据库 | 通用缩写，不建页 |
| UI | User Interface | 用户界面 | 通用缩写，不建页 |
| OS | Operating System | 操作系统 | 通用缩写，不建页 |
| ID | Identifier | 标识符 | 通用缩写，不建页 |
| RFC | Request for Comments | 互联网标准草案 | 通用缩写，不建页 |
| SSO | Single Sign-On | 单点登录 | 通用缩写，不建页 |
| RBAC | Role-Based Access Control | 基于角色的访问控制 | 通用缩写，不建页 |
| GET | HTTP GET | HTTP GET 方法 | 通用缩写，不建页 |
| POST | HTTP POST | HTTP POST 方法 | 通用缩写，不建页 |
| CONNECT | HTTP CONNECT | HTTP CONNECT 方法 | 通用缩写，不建页 |
| VIP | Virtual IP | 虚拟 IP | 通用缩写，不建页 |
| L3 | Layer 3 | 网络层 | 通用缩写，不建页 |
| DR | Disaster Recovery | 灾难恢复 | 通用缩写，不建页 |
| CA | Certificate Authority | 证书颁发机构 | 通用缩写，不建页 |
| SAN | Subject Alternative Name | 证书备用名称 | 通用缩写，不建页 |
| AZ | Availability Zone | 可用区 | 通用缩写，不建页 |
| GW | Gateway | 网关 | 通用缩写，不建页 |
| PR | Pull Request | 拉取请求 | 通用缩写，不建页 |

## 保留英文（不翻译）

AWS KMS · OpenBao · agentgateway · MCP · OWASP · CB4A · RFC 8693 · RFC 7523 ·
JWT · OAuth · STS · IdP · PostgreSQL · SQLite · mermaid

## 已知劣质译法（机翻件里的，禁止使用）

经纪 · 经纪人 · 凭证经纪 · KMS 资助 · 快速信封加密 · 代表流 · 困惑的副手 · 自治度 · 人在回路
