# ipdftoo-api

[iPDFToo（ilovepdftoo.cn）](https://www.ilovepdftoo.cn) 的服务端源码（FastAPI / Python 3.12），为本站在线 PDF/OFD 工具提供后端 API。

## 为什么这个仓库是公开的

本服务端组合使用了 [PyMuPDF](https://pymupdf.readthedocs.io/)（AGPL-3.0，Artifex Software）。依照 AGPL-3.0 第 13 条（网络交互条款），向网络用户提供服务的组合作品须向这些用户提供源码获取途径——**本仓库即为此目的而设的 source offer**，与线上运行的版本保持同步（`master` 实时对应主仓库的 `apps/api` 子树）。

本仓库自身代码以 **AGPL-3.0** 提供（见 [LICENSE](./LICENSE)）。

## 技术栈

- FastAPI + Uvicorn（Gunicorn 部署）
- PDF 处理：PyMuPDF、pikepdf、pypdf、pdf2docx、pymupdf4llm
- OFD 处理：easyofd
- Office 转换：MarkItDown、LibreOffice（系统依赖）
- 图像：Pillow、pypdfium2

## 仓库边界说明

- 本仓库**只含服务端**（AGPL 组合作品边界即 `apps/api`）。前端与小程��不在组合作品范围内，未包含于此。
- 不含任何密钥与环境配置：`.env`、SSL 证书、生产配置均不入库。
- 文件处理即用即删，无任何用户数据保留逻辑。

## 致谢

- [PyMuPDF / Artifex Software](https://artifex.com/) — PDF 渲染与处理核心
- [FastAPI](https://fastapi.tiangolo.com/) 及各依赖库的开源社区

## 联系

javafx@163.com · [iPDFToo](https://www.ilovepdftoo.cn)
