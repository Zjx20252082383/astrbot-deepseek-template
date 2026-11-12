#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音闭环补丁 —— 应用 / 回滚（两个文件）

目标：
  1. modules/voice.py   逐段遍历转写 + 空段占位 [此处转写缺失] + 语气词过滤
  2. modules/memory.py  占位标记进固结前剔除（不让它被当成用户真说过的话写进记忆）

为什么不用 patch 工具：
  voice.py 是 CRLF、memory.py 是 LF，两个文件行尾不同；
  这里按原字节替换，各自保留自己的换行风格。

用法：
    python voice_multiseg_apply.py status
    python voice_multiseg_apply.py apply
    python voice_multiseg_apply.py rollback
    python voice_multiseg_apply.py rollback --force     # 应用后又被手工改过时

rollback 的 sha 保护：
  apply 时会把"应用后的 sha"写进 <文件>.applied.sha。rollback 前先比对这个值，
  若当前文件已经不是应用后的状态（说明中间有热修），默认拒绝覆盖，只能用 --force。
  回滚后会再校验一次：结果必须等于备份文件的 sha。
"""

import hashlib
import shutil
import sys
from pathlib import Path

PLUGIN = Path(r"E:\Users\zjx\Desktop\deepseek接QQ小号\data\plugins\astrbot_plugin_shihai_behavior")
VOICE = PLUGIN / "modules" / "voice.py"
MEM = PLUGIN / "modules" / "memory.py"

MARKER = "[此处转写缺失]"

# ============ voice.py（CRLF）============
V_EOL = "\r\n"

V_ANCHOR = "    # ---------------- 听懂（STT 拦截） -----------------------------" + V_EOL

V_METHOD = V_EOL.join([
    '    # 语气词/停顿词：只用于判断"整段转写有没有实义"，不用于过滤正常句子',
    '    _FILLER_WORDS = ("就是那个", "就是", "那个", "然后", "那么",',
    '                     "嗯", "啊", "哦", "额", "呃", "唔", "唉", "呀")',
    '',
    '    def _is_noise_transcript(self, text: str) -> bool:',
    '        """转写是否没有实义（过短，或只剩下语气词）。',
    '',
    '        不按字数一刀切："没汤，饭有。"去掉标点只有 4 个字，但是真实信息；',
    '        "就是那个然后"有 6 个字，却全是语气词。所以短句要看去掉语气词后还剩多少。',
    '        """',
    '        t = re.sub(r"[\\s，。！？、,.!?…\\-—_\\"\'（）()【】\\[\\]:：；;]", "", text or "")',
    '        if not t:',
    '            return True',
    '        t2 = re.sub("|".join(re.escape(w) for w in self._FILLER_WORDS), "", t)',
    '        if len(t) < 5:',
    '            return len(t2) < 2',
    '        return len(t2) < 3',
    '',
]) + V_EOL

V_OLD_BLOCK = V_EOL.join([
    '        file_path = await self._download_record(event, record_segs[0])',
    '        if not file_path:',
    '            logger.warning("[voice] 语音文件没取到，跳过识别")',
    '            return',
    '        text = await self._transcribe(file_path)',
    '        try:',
    '            os.unlink(file_path)',
    '        except Exception:',
    '            pass',
    '        if not text:',
    '            logger.warning("[voice] 语音转文字失败（无可用 STT 后端），跳过")',
    '            return',
]) + V_EOL

V_NEW_BLOCK = V_EOL.join([
    '        # 逐段转写：一条消息可能带多个语音段，只取第一段会丢内容',
    '        missing = "[此处转写缺失]"',
    '        parts: list[str] = []',
    '        for idx, seg in enumerate(record_segs, 1):',
    '            seg_path = await self._download_record(event, seg)',
    '            if not seg_path:',
    '                parts.append("")',
    '                logger.warning(f"[voice] 第 {idx}/{len(record_segs)} 段语音没取到文件")',
    '                continue',
    '            seg_text = await self._transcribe(seg_path)',
    '            try:',
    '                os.unlink(seg_path)',
    '            except Exception:',
    '                pass',
    '            parts.append((seg_text or "").strip())',
    '        # 空段用占位标记保留位置：段与段的对应关系不能因为某段失败而错位',
    '        joined = "\\n".join(p if p else missing for p in parts)',
    '        if all(not p for p in parts):',
    '            logger.warning(f"[voice] {len(parts)} 段语音全部未转写成功（ASR 未返回文本），跳过")',
    '            return',
    '        if self._is_noise_transcript(joined.replace(missing, "")):',
    '            logger.warning("[voice] 语音转写仅语气词或过短，跳过")',
    '            return',
]) + V_EOL

V_OLD_EXTRA = '        extra_plain = f"\\n{hint}：{text}"' + V_EOL
V_NEW_EXTRA = '        extra_plain = f"\\n{hint}：{joined}"' + V_EOL
V_OLD_LOG = '        logger.info(f"[voice] 听懂了: {text}")' + V_EOL
V_NEW_LOG = '        logger.info(f"[voice] 听懂了（{len(parts)} 段）: {joined}")' + V_EOL

# ============ memory.py（LF）============
M_EOL = "\n"

M_OLD = '        return text.strip()' + M_EOL
M_NEW = M_EOL.join([
    '        # 语音的占位标记只给当轮对话的 LLM 看：进长期记忆（固结）前必须去掉，',
    '        # 否则"[此处转写缺失]"会被当成用户真说过的话写进事件/事实',
    '        return text.replace("[此处转写缺失]", "").strip()',
]) + M_EOL


def sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def sha_file(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".applied.sha")


def bak_file(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".bak_prev")


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def write(p: Path, text: str) -> None:
    p.write_bytes(text.encode("utf-8"))


def voice_forward(text: str) -> str:
    for frag in (V_ANCHOR, V_OLD_BLOCK, V_OLD_EXTRA, V_OLD_LOG):
        if text.count(frag) != 1:
            raise SystemExit(f"[中止] voice.py 定位失败（{frag[:36]!r} 命中 {text.count(frag)} 次）")
    text = text.replace(V_ANCHOR, V_METHOD + V_ANCHOR)
    text = text.replace(V_OLD_BLOCK, V_NEW_BLOCK)
    text = text.replace(V_OLD_EXTRA, V_NEW_EXTRA)
    return text.replace(V_OLD_LOG, V_NEW_LOG)


def voice_backward(text: str) -> str:
    for frag in (V_METHOD, V_NEW_BLOCK, V_NEW_EXTRA, V_NEW_LOG):
        if text.count(frag) != 1:
            raise SystemExit(f"[中止] voice.py 反查失败（{frag[:36]!r} 命中 {text.count(frag)} 次）")
    text = text.replace(V_NEW_LOG, V_OLD_LOG)
    text = text.replace(V_NEW_EXTRA, V_OLD_EXTRA)
    text = text.replace(V_NEW_BLOCK, V_OLD_BLOCK)
    return text.replace(V_METHOD, "")


def status_of(p: Path, applied_probe: str, rolled_probe: str) -> str:
    t = read(p)
    if t.count(applied_probe) == 1:
        return "已应用"
    if t.count(rolled_probe) == 1:
        return "原始状态"
    return "未知（两态都对不上，可能被手工改过）"


def do_apply() -> None:
    plans = [
        (VOICE, voice_forward, V_NEW_BLOCK, V_OLD_BLOCK),
        (MEM, lambda t: t.replace(M_OLD, M_NEW) if t.count(M_OLD) == 1 else (_ for _ in ()).throw(
            SystemExit("[中止] memory.py 定位失败")), M_NEW, M_OLD),
    ]
    for p, forward, new_probe, old_probe in plans:
        text = read(p)
        if text.count(new_probe) == 1:
            print(f"[跳过] {p.name} 已是改后状态")
            continue
        if not bak_file(p).exists():
            shutil.copy2(p, bak_file(p))
            print(f"[备份] {bak_file(p).name}  sha={sha16(p)}")
        new = forward(text)
        write(p, new)
        sha_file(p).write_text(sha16(p), encoding="utf-8")
        print(f"[应用] {p.name}  sha {sha16(bak_file(p))} -> {sha16(p)}")


def do_rollback(force: bool) -> None:
    for p in (VOICE, MEM):
        bak = bak_file(p)
        if not bak.exists():
            print(f"[跳过] {p.name} 没有 .bak_prev，未做处理")
            continue
        cur, want = sha16(p), sha_file(p)
        if want.exists() and cur != want.read_text(encoding="utf-8").strip() and not force:
            print(f"[拒绝] {p.name} 应用后又被改过（当前 {cur} ≠ 记录 {want.read_text().strip()}），"
                  f"回滚会覆盖这些改动。确认要覆盖请加 --force")
            continue
        shutil.copy2(bak, p)
        got, expect = sha16(p), sha16(bak)
        print(f"[回滚] {p.name}  sha={got}  与备份一致={got == expect}")


def do_status() -> None:
    print(f"voice.py : {status_of(VOICE, V_NEW_BLOCK, V_OLD_BLOCK)}  sha={sha16(VOICE)}")
    print(f"memory.py: {status_of(MEM, M_NEW, M_OLD)}  sha={sha16(MEM)}")
    for p in (VOICE, MEM):
        w = sha_file(p)
        if w.exists():
            print(f"  {p.name}.applied.sha = {w.read_text(encoding='utf-8').strip()}")


def main() -> None:
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    force = "--force" in sys.argv
    if action == "status":
        do_status()
    elif action == "apply":
        do_apply()
        print()
        do_status()
    elif action == "rollback":
        do_rollback(force)
        print()
        do_status()
    else:
        print(f"未知动作：{action}（可用：status / apply / rollback [--force]）")
        sys.exit(1)


if __name__ == "__main__":
    main()
