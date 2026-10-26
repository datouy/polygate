"""polygate 启动入口。

用法：
    python run.py

可通过环境变量自定义：
    POLYGATE_CONFIG  配置文件路径（默认 config.example.yaml）
    POLYGATE_PORT    监听端口（默认 8000）
"""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("POLYGATE_PORT", "8000"))
    uvicorn.run(
        "polygate.server:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        reload=False,
    )
