from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageOps, ImageSequence

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    from astrbot.core.message import components as Comp  # type: ignore


class MemeSymmetryPlugin(Star):
    """引用图片或直接带图后，生成左右对称 meme 图。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.temp_root = Path("data/temp")
        self.temp_root.mkdir(parents=True, exist_ok=True)

    @filter.command("对称")
    async def symmetry(self, event: AstrMessageEvent, side: str = ""):
        """
        用法：
        /对称 左
        /对称 右
        """
        side = (side or "").strip()
        if side not in {"左", "右"}:
            yield event.plain_result("用法：/对称 左 或 /对称 右\n支持：直接带图 / 引用图片 / 静态图 / GIF")
            event.stop_event()
            return

        try:
            src_path = await self._extract_target_image(event)
            if not src_path:
                yield event.plain_result("没有找到图片。请直接带图发送，或先引用一张图片再发送：/对称 左")
                event.stop_event()
                return

            out_path = await asyncio.to_thread(self._render, src_path, side)
            yield event.chain_result([Comp.Image.fromFileSystem(str(out_path))])
            event.stop_event()

            # 延时删除临时文件，避免协议端还没来得及读取
            asyncio.create_task(self._cleanup_later(src_path))
            asyncio.create_task(self._cleanup_later(out_path))

        except Exception as e:
            yield event.plain_result(f"生成失败：{e}")
            event.stop_event()

    async def _extract_target_image(self, event: AstrMessageEvent) -> Optional[Path]:
        chain = list(getattr(event.message_obj, "message", None) or [])

        # 1) 优先读取当前消息直接附带的图片
        for comp in chain:
            path = await self._image_component_to_path(comp)
            if path:
                return path

        # 2) 再读取引用消息中的图片
        for comp in chain:
            if not self._is_reply_component(comp):
                continue

            for reply_comp in self._iter_reply_chain(comp):
                path = await self._image_component_to_path(reply_comp)
                if path:
                    return path

        return None

    def _is_reply_component(self, comp) -> bool:
        reply_cls = getattr(Comp, "Reply", None)
        if reply_cls is not None and isinstance(comp, reply_cls):
            return True

        comp_type = getattr(comp, "type", None)
        if str(comp_type).lower().endswith("reply"):
            return True

        return comp.__class__.__name__ == "Reply"

    def _iter_reply_chain(self, reply_comp) -> Iterable[object]:
        """
        Reply 组件在不同版本 / 不同插件代码里，常见结构有：
        - reply.chain
        - reply.message
        - reply.source.message_chain

        这里都兼容一下，避免后续你移植别的图处理时还要重复写。
        """
        direct_chain = getattr(reply_comp, "chain", None)
        if isinstance(direct_chain, list):
            for item in direct_chain:
                yield item
            return

        direct_message = getattr(reply_comp, "message", None)
        if isinstance(direct_message, list):
            for item in direct_message:
                yield item
            return

        source = getattr(reply_comp, "source", None)
        source_chain = getattr(source, "message_chain", None)
        if isinstance(source_chain, list):
            for item in source_chain:
                yield item

    async def _image_component_to_path(self, comp) -> Optional[Path]:
        if not self._is_image_component(comp):
            return None

        # 官方/社区插件里常用的方式
        convert = getattr(comp, "convert_to_file_path", None)
        if callable(convert):
            result = convert()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                p = Path(str(result))
                if p.exists():
                    return p

        # 兜底字段
        for key in ("file", "path", "url"):
            value = getattr(comp, key, None)
            if not value:
                continue

            p = Path(str(value))
            if p.exists():
                return p

        return None

    def _is_image_component(self, comp) -> bool:
        image_cls = getattr(Comp, "Image", None)
        if image_cls is not None and isinstance(comp, image_cls):
            return True

        comp_type = getattr(comp, "type", None)
        if str(comp_type).lower().endswith("image"):
            return True

        return comp.__class__.__name__ == "Image"

    def _render(self, src_path: Path, side: str) -> Path:
        with Image.open(src_path) as im:
            is_animated = bool(getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1)

        ext = ".gif" if is_animated else ".png"
        out_path = self.temp_root / f"astrbot_plugin_meme_symmetry_{uuid.uuid4().hex}{ext}"

        if is_animated:
            self._render_animated(src_path, out_path, side)
        else:
            self._render_static(src_path, out_path, side)

        return out_path

    def _render_static(self, src_path: Path, out_path: Path, side: str) -> None:
        with Image.open(src_path) as im:
            fixed = ImageOps.exif_transpose(im).convert("RGBA")
            result = self._apply_symmetry(fixed, side)
            result.save(out_path, format="PNG")

    def _render_animated(self, src_path: Path, out_path: Path, side: str) -> None:
        with Image.open(src_path) as im:
            frames = []
            durations = []
            loop = im.info.get("loop", 0)

            for frame in ImageSequence.Iterator(im):
                rgba = frame.convert("RGBA")
                result = self._apply_symmetry(rgba, side)
                frames.append(result)
                durations.append(frame.info.get("duration", im.info.get("duration", 80)))

            if not frames:
                raise RuntimeError("GIF 没有可处理帧")

            frames[0].save(
                out_path,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                loop=loop,
                duration=durations,
                disposal=2,
                optimize=False,
            )

    def _apply_symmetry(self, img: Image.Image, side: str) -> Image.Image:
        img = img.convert("RGBA")
        width, height = img.size
        canvas = Image.new("RGBA", (width, height))

        half_width = (width + 1) // 2

        if side == "左":
            left = img.crop((0, 0, half_width, height))
            mirrored = ImageOps.mirror(left)
            canvas.paste(left, (0, 0))
            canvas.paste(mirrored, (width - half_width, 0))
        else:
            right = img.crop((width - half_width, 0, width, height))
            mirrored = ImageOps.mirror(right)
            canvas.paste(mirrored, (0, 0))
            canvas.paste(right, (width - half_width, 0))

        return canvas

    async def _cleanup_later(self, path: Path, delay: int = 120) -> None:
        await asyncio.sleep(delay)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
