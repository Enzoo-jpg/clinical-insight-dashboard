# 临床用药洞察数据看板（Streamlit 版）

服务器端 pandas 计算，几万~十万行数据秒级出结果。功能与原纯前端版口径完全一致：
医生四象限分层、机会市场（含/未含本品趋势）、医院与品种/适应症分析、一键导出 Excel。

## 本地运行

```bash
pip install -r requirements.txt
streamlit run app.py
```

浏览器打开 http://localhost:8501 。点击「加载示例数据」即可体验，或上传自己的 Excel/CSV。

## 部署到 Streamlit Community Cloud（免费）

1. 把本目录下的文件（`app.py`、`compute.py`、`requirements.txt`、`.streamlit/`）上传到 GitHub 仓库根目录。
2. 打开 https://share.streamlit.io ，用 GitHub 账号登录。
3. 点「New app」→ 选择仓库、分支（main）、主文件填 `app.py` → Deploy。
4. 等待 1~3 分钟构建完成，即可获得公开访问链接。

## 文件说明

| 文件 | 作用 |
|---|---|
| `app.py` | Streamlit 前端与交互 |
| `compute.py` | 计算引擎（pandas 向量化） |
| `requirements.txt` | 依赖清单 |
| `.streamlit/config.toml` | 主题与上传大小配置 |
