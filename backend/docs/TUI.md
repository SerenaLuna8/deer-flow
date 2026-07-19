# Terminal UI

TUI 是本地嵌入式交互界面。CLI 本身没有认证项目上下文，因此默认会话是无持久化的：它不能列出、续接或写入 Web 项目的 Thread。

```bash
make tui
```

可信 embedding 如需持久化，必须显式提供不可变 `PrivateResourceScope`、Agent asset ID、Agent scope 和项目 scoped checkpointer。缺少任一 authority 时读写失败关闭；不存在默认用户、owner-only repository 或全局 saver fallback。

生产多用户工作请使用 Web 项目界面或项目 API。
