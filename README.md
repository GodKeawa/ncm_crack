# NCM 文件解密工具

将网易云音乐加密的 `.ncm` 文件转换为标准音频格式（MP3/FLAC），并保留完整的元数据和专辑封面。

## 功能特性

- **自动格式识别**：根据文件头自动检测输出格式（MP3 或 FLAC）
- **完整元数据**：自动提取并写入歌曲名、艺术家、专辑、年份等信息
- **专辑封面**：自动下载并嵌入专辑封面图片
- **多格式支持**：同时支持 MP3 和 FLAC 格式的元数据写入
- **批量转换**：支持目录批量转换，自动跳过已转换文件
- **多线程处理**：利用多核 CPU 提升转换速度

## 使用方法

```bash
# 基本用法（输出到 Music/output 目录）
python ncm_crack.py -p ./Music

# 指定输出目录
python ncm_crack.py -p ./Music -o ./Output
```

## 依赖说明

主要依赖库：
- `pycryptodome` - 文件解密
- `mutagen` - 元数据处理（支持 MP3 和 FLAC）
- `requests` - 封面下载
- `tqdm` - 进度显示
- `psutil` - CPU 使用率控制

安装依赖：
```bash
uv sync
```
