"""NCM 文件解密工具

支持将网易云音乐加密的 .ncm 文件转换为标准音频格式（MP3/FLAC）
"""

import argparse
import base64
import binascii
import json
import os
import struct
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional, Tuple

import eyed3
import psutil
import requests
from Crypto.Cipher import AES
from Crypto.Util.strxor import strxor
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC
from mutagen.mp3 import MP3
from tqdm import tqdm

# ==================== 常量定义 ====================
CORE_KEY = binascii.a2b_hex("687A4852416D736F356B496E62617857")
META_KEY = binascii.a2b_hex("2331346C6A6B5F215C5D2630553C2728")
NCM_HEADER = b"4354454e4644414d"
CHUNK_SIZE = 0x8000  # 32KB
MAX_RETRIES = 3
RETRY_DELAY = 5
MAX_CPU_PERCENT = 100
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36"
)


# ==================== 工具函数 ====================
def unpad(data: bytes) -> bytes:
    """移除 PKCS7 填充"""
    padding_len = data[-1] if isinstance(data[-1], int) else ord(data[-1])
    return data[:-padding_len]


def detect_audio_format(data: bytes) -> str:
    """通过文件头检测音频格式

    Args:
        data: 音频文件的前几个字节

    Returns:
        'flac' 或 'mp3'
    """
    if len(data) >= 4 and data.startswith(b"fLaC"):
        return "flac"
    elif len(data) >= 3 and data.startswith(b"ID3"):
        return "mp3"
    elif len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return "mp3"  # 默认返回 mp3


def download_image(url: str, save_path: str, max_retries: int = MAX_RETRIES) -> bool:
    """下载图片并保存

    Args:
        url: 图片 URL
        save_path: 保存路径
        max_retries: 最大重试次数

    Returns:
        是否下载成功
    """
    headers = {"User-Agent": DEFAULT_USER_AGENT}

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            with open(save_path, "wb") as f:
                f.write(response.content)
            return True

        except Exception as e:
            if attempt < max_retries - 1:
                print(f"图片下载失败（尝试 {attempt + 1}/{max_retries}），等待重试...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"图片下载失败: {e}")
                return False

    return False


def set_mp3_metadata(
    mp3_path: str, metadata: dict, cover_data: Optional[bytes] = None
) -> bool:
    """为 MP3 文件设置完整元数据和封面

    Args:
        mp3_path: MP3 文件路径
        metadata: 元数据字典
        cover_data: 封面图片数据（字节）

    Returns:
        是否设置成功
    """
    try:
        # 使用 mutagen 设置元数据
        audio = MP3(mp3_path, ID3=ID3)

        # 添加或创建 ID3 标签
        try:
            audio.add_tags()
        except Exception:
            pass  # 标签已存在

        # 设置基本元数据
        if "musicName" in metadata:
            audio.tags["TIT2"] = TIT2(encoding=3, text=metadata["musicName"])

        if "artist" in metadata:
            artists = metadata["artist"]
            if isinstance(artists, list):
                artist_names = [
                    a[0] if isinstance(a, list) and a else str(a) for a in artists
                ]
                audio.tags["TPE1"] = TPE1(encoding=3, text="/".join(artist_names))
            else:
                audio.tags["TPE1"] = TPE1(encoding=3, text=str(artists))

        if "album" in metadata:
            audio.tags["TALB"] = TALB(encoding=3, text=metadata["album"])

        if "publishTime" in metadata:
            year = str(metadata["publishTime"])[:4]  # 提取年份
            audio.tags["TDRC"] = TDRC(encoding=3, text=year)

        # 添加封面
        if cover_data:
            audio.tags["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Cover (front)
                desc="Cover",
                data=cover_data,
            )

        audio.save()
        return True

    except Exception as e:
        warnings.warn(f"设置 MP3 元数据失败: {e}")
        return False


def set_flac_metadata(
    flac_path: str, metadata: dict, cover_data: Optional[bytes] = None
) -> bool:
    """为 FLAC 文件设置完整元数据和封面

    Args:
        flac_path: FLAC 文件路径
        metadata: 元数据字典
        cover_data: 封面图片数据（字节）

    Returns:
        是否设置成功
    """
    try:
        audio = FLAC(flac_path)

        # 设置基本元数据 (Vorbis Comments)
        if "musicName" in metadata:
            audio["TITLE"] = metadata["musicName"]

        if "artist" in metadata:
            artists = metadata["artist"]
            if isinstance(artists, list):
                artist_names = [
                    a[0] if isinstance(a, list) and a else str(a) for a in artists
                ]
                audio["ARTIST"] = "/".join(artist_names)
            else:
                audio["ARTIST"] = str(artists)

        if "album" in metadata:
            audio["ALBUM"] = metadata["album"]

        if "publishTime" in metadata:
            year = str(metadata["publishTime"])[:4]
            audio["DATE"] = year

        # 添加封面
        if cover_data:
            picture = Picture()
            picture.type = 3  # Cover (front)
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = cover_data

            # 清除现有封面
            audio.clear_pictures()
            audio.add_picture(picture)

        audio.save()
        return True

    except Exception as e:
        warnings.warn(f"设置 FLAC 元数据失败: {e}")
        return False


def set_audio_metadata(audio_path: str, metadata: dict) -> bool:
    """为音频文件设置完整元数据和封面（支持 MP3 和 FLAC）

    Args:
        audio_path: 音频文件路径
        metadata: 元数据字典

    Returns:
        是否设置成功
    """
    audio_path = Path(audio_path)
    file_format = audio_path.suffix.lower()[1:]  # 移除点号

    # 下载封面图片
    cover_data = None
    cover_url = metadata.get("albumPic", "")

    if cover_url:
        # 支持 JPG 和 PNG 格式
        if cover_url.lower().endswith((".jpg", ".jpeg", ".png")):
            temp_cover = audio_path.parent / f".temp_{audio_path.stem}_cover.jpg"
            try:
                if download_image(cover_url, str(temp_cover)):
                    with open(temp_cover, "rb") as f:
                        cover_data = f.read()
                    temp_cover.unlink()
            except Exception as e:
                warnings.warn(f"下载封面失败: {e}")
                if temp_cover.exists():
                    temp_cover.unlink()
        else:
            warnings.warn(f"不支持的封面格式: {cover_url}")

    # 根据格式设置元数据
    try:
        if file_format == "mp3":
            return set_mp3_metadata(str(audio_path), metadata, cover_data)
        elif file_format == "flac":
            return set_flac_metadata(str(audio_path), metadata, cover_data)
        else:
            warnings.warn(f"不支持的音频格式: {file_format}")
            return False
    except Exception as e:
        warnings.warn(f"设置元数据失败: {e}")
        return False


# ==================== 核心解密类 ====================
class NCMDecryptor:
    """NCM 文件解密器"""

    def __init__(self, input_path: str):
        self.input_path = Path(input_path)
        self.metadata = None
        self.key_box = None

    def _read_key_data(self, file) -> bytes:
        """读取并解密密钥数据"""
        file.seek(2, 1)  # 跳过2字节
        key_length = struct.unpack("<I", file.read(4))[0]

        # XOR 解密
        key_data = bytearray(file.read(key_length))
        for i in range(len(key_data)):
            key_data[i] ^= 0x64

        # AES 解密
        cipher = AES.new(CORE_KEY, AES.MODE_ECB)
        return unpad(cipher.decrypt(bytes(key_data)))[17:]

    def _build_key_box(self, key_data: bytes) -> bytearray:
        """构建密钥盒"""
        key_box = bytearray(range(256))
        key_length = len(key_data)
        last_byte = 0
        key_offset = 0

        for i in range(256):
            swap = key_box[i]
            c = (swap + last_byte + key_data[key_offset]) & 0xFF
            key_offset = (key_offset + 1) % key_length
            key_box[i], key_box[c] = key_box[c], swap
            last_byte = c

        return key_box

    def _read_metadata(self, file) -> dict:
        """读取并解密元数据"""
        meta_length = struct.unpack("<I", file.read(4))[0]

        # XOR 解密
        meta_data = bytearray(file.read(meta_length))
        for i in range(len(meta_data)):
            meta_data[i] ^= 0x63

        # Base64 + AES 解密
        meta_data = base64.b64decode(bytes(meta_data)[22:])
        cipher = AES.new(META_KEY, AES.MODE_ECB)
        meta_json = unpad(cipher.decrypt(meta_data)).decode("utf-8")[6:]

        return json.loads(meta_json)

    def _create_decryption_mask(self) -> bytes:
        """创建解密掩码"""
        mask = bytearray(256)
        for i in range(256):
            j = (i + 1) & 0xFF
            mask[i] = self.key_box[
                (self.key_box[j] + self.key_box[(self.key_box[j] + j) & 0xFF]) & 0xFF
            ]
        return bytes(mask) * (CHUNK_SIZE // 256)

    def _skip_image_data(self, file):
        """跳过嵌入的图片数据"""
        file.read(4)  # CRC32
        file.seek(5, 1)  # 跳过5字节
        image_size = struct.unpack("<I", file.read(4))[0]
        file.seek(image_size, 1)  # 跳过图片数据

    def decrypt(self, output_path: str) -> Tuple[str, Optional[dict]]:
        """解密 NCM 文件

        Args:
            output_path: 输出文件路径（可能会根据实际格式调整）

        Returns:
            (实际输出路径, 元数据字典)
        """
        with open(self.input_path, "rb") as f:
            # 验证文件头
            header = f.read(8)
            if binascii.b2a_hex(header) != NCM_HEADER:
                raise ValueError("不是有效的 NCM 文件")

            # 读取密钥和元数据
            key_data = self._read_key_data(f)
            self.key_box = self._build_key_box(key_data)
            self.metadata = self._read_metadata(f)

            # 跳过图片数据
            self._skip_image_data(f)

            # 创建解密掩码
            full_mask = self._create_decryption_mask()

            # 解密音频数据并检测格式
            output_path = Path(output_path)
            temp_path = output_path.with_suffix(".tmp")
            actual_format = self.metadata.get("format", "mp3")

            with open(temp_path, "wb") as out:
                first_chunk = True

                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    # 解密
                    chunk_len = len(chunk)
                    decrypted = strxor(chunk, full_mask[:chunk_len])

                    # 检测实际格式
                    if first_chunk:
                        actual_format = detect_audio_format(decrypted)
                        first_chunk = False

                    out.write(decrypted)

            # 重命名为正确的扩展名
            final_path = output_path.with_suffix(f".{actual_format}")
            if final_path.exists():
                final_path.unlink()
            temp_path.rename(final_path)

            return str(final_path), self.metadata


# ==================== 批量转换器 ====================
class BatchConverter:
    """批量转换 NCM 文件"""

    def __init__(self, input_dir: str, output_dir: Optional[str] = None):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir) if output_dir else self.input_dir / "output"
        self.output_dir.mkdir(exist_ok=True)

    def _is_already_converted(self, base_name: str) -> bool:
        """检查文件是否已转换"""
        base_path = self.output_dir / base_name
        return (
            base_path.with_suffix(".mp3").exists()
            or base_path.with_suffix(".flac").exists()
        )

    def _convert_single_file(
        self, ncm_path: Path, max_retries: int = 5
    ) -> Optional[bool]:
        """转换单个文件

        Returns:
            True: 成功, False: 失败, None: 跳过
        """
        if ncm_path.suffix.lower() != ".ncm":
            return None

        # 检查是否已转换
        base_name = ncm_path.stem
        if self._is_already_converted(base_name):
            return None

        # 多次重试
        for attempt in range(max_retries):
            try:
                # CPU 使用率控制
                while psutil.cpu_percent(1) > MAX_CPU_PERCENT:
                    time.sleep(0.5)

                # 解密文件
                output_path = self.output_dir / f"{base_name}.mp3"  # 临时扩展名
                decryptor = NCMDecryptor(str(ncm_path))
                final_path, metadata = decryptor.decrypt(str(output_path))

                # 设置元数据和封面
                if metadata:
                    set_audio_metadata(final_path, metadata)

                return True

            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(3)
                else:
                    print(f"转换失败: {ncm_path.name} - {e}")
                    return False

        return False

    def convert_all(self, max_workers: Optional[int] = None) -> dict:
        """批量转换所有 NCM 文件

        Args:
            max_workers: 最大线程数，默认为 CPU 核心数的 80%

        Returns:
            统计字典 {'success': int, 'failed': int, 'skipped': int}
        """
        # 收集所有 NCM 文件
        ncm_files = list(self.input_dir.glob("*.ncm"))

        if not ncm_files:
            print(f"在 {self.input_dir} 中未找到 NCM 文件")
            return {"success": 0, "failed": 0, "skipped": 0}

        # 确定线程数
        if max_workers is None:
            cpu_count = os.cpu_count() or 1
            max_workers = max(1, int(cpu_count * 0.8))

        stats = {"success": 0, "failed": 0, "skipped": 0}

        # 并行转换
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._convert_single_file, ncm_file)
                for ncm_file in ncm_files
            ]

            # 显示进度
            with tqdm(total=len(futures), desc="转换进度", unit="文件") as pbar:
                for future in futures:
                    future.add_done_callback(lambda _: pbar.update(1))
                wait(futures)

            # 统计结果
            for future in futures:
                result = future.result()
                if result is True:
                    stats["success"] += 1
                elif result is False:
                    stats["failed"] += 1
                else:
                    stats["skipped"] += 1

        return stats


# ==================== 命令行入口 ====================
def main():
    """命令行主函数"""
    parser = argparse.ArgumentParser(
        description="NCM Cracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("-p", "--path", required=True, help="包含 NCM 文件的目录路径")

    parser.add_argument(
        "-o", "--output", help="输出目录路径（默认为输入目录下的 output 子目录）"
    )

    args = parser.parse_args()

    # 验证输入路径
    if not os.path.isdir(args.path):
        print(f"错误: 路径不存在或不是目录: {args.path}")
        return

    # 执行转换
    print(f"输入目录: {args.path}")
    converter = BatchConverter(args.path, args.output)
    print(f"输出目录: {converter.output_dir}")
    print()

    stats = converter.convert_all()

    # 显示统计
    print(f"\n转换完成!")
    print(f"  成功: {stats['success']} 个文件")
    print(f"  失败: {stats['failed']} 个文件")
    print(f"  跳过: {stats['skipped']} 个文件")


if __name__ == "__main__":
    main()
