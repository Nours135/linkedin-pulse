# linkedin-pulse

```
main.py                    # heartbeat 循环
config.yaml                # 轮训间隔、搜索地点、匹配阈值
.env.example               # API key / SMTP
assets/
  resume.json              # 用户 JSON 简历（示例：John Wang）
  resume.tex.j2            # LaTeX 简历模板
work/                      # 运行产物（gitignored）
pulse/
  config.py                # 配置、LLM 客户端
  state.py                 # LangGraph State
  store.py                 # 已处理岗位去重
  graph.py                 # entry node → job agent
  agents/
    job_agent.py           # 岗位轮训 agent
    resume_agent.py        # 简历组装 agent
  tools/
    linkedin.py            # LinkedIn 搜索
    matching.py            # 关键词 / 粗筛 / 匹配度
    latex.py               # LaTeX 渲染编译
    email.py               # 邮件生成与发送
```
