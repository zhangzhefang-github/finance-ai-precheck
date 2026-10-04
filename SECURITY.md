# Security Policy

## Supported scope

当前仓库是本地技术 POC，没有生产服务或正式版本支持承诺。安全修复只针对当前 `main` 分支。

## Reporting a vulnerability

请通过仓库所有者的 GitHub 私有联系方式报告安全问题。不要在公开 Issue 中粘贴凭证、签名 URL、真实票据、个人信息、数据库或可利用细节。

如果凭证曾进入提交、日志、Issue 或聊天记录，应立即在相应服务端吊销并轮换；仅从 Git 历史删除不能使已暴露凭证恢复安全。

## Repository data rules

- `.env`、`var/`、上传材料、解析工件、运行数据库和评测产物不得提交。
- 示例和测试只使用合成数据。
- COS/MinerU/模型调用必须由用户显式触发，并从本地环境变量读取配置。
- 公开日志和错误信息不得包含 Secret、Token 或带查询签名的 URL。
