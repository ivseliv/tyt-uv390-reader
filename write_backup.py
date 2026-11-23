"""Private, versioned journal for a stock settings write. It is never a device command."""
import hashlib
import hmac
import os
from pathlib import Path
import struct
import tempfile

from rdt_editor import publish_new


MAGIC = b"TYTBAK1\0"
HEADER = struct.Struct("<8s8sI")
BLOCK_SIZE = 1024
SECTOR_SIZE = 65536
MAX_BLOCKS = 710
MAX_SIZE = HEADER.size + MAX_BLOCKS * (4 + 2 * BLOCK_SIZE) + 32
MODELS = (b"MD-UV380", b"MD-UV390")


def save_backup(path, model, blocks):
    """Publish the complete journal before the native writer is allowed to open USB."""
    path = Path(path)
    model_bytes = model.encode("ascii")
    if model_bytes not in MODELS or not 0 < len(blocks) <= MAX_BLOCKS:
        raise ValueError("Неподдерживаемая резервная копия.")
    payload = bytearray(HEADER.pack(MAGIC, model_bytes, len(blocks)))
    for address, (before, after) in sorted(blocks.items()):
        if address % BLOCK_SIZE or len(before) != BLOCK_SIZE or len(after) != BLOCK_SIZE:
            raise ValueError("Неверная длина блока резервной копии.")
        payload.extend(struct.pack("<I", address))
        payload.extend(before)
        payload.extend(after)
    payload.extend(hashlib.sha256(payload).digest())
    if os.path.lexists(path):
        raise FileExistsError("Выберите новое имя: выходной файл уже существует.")
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".tyt-backup-") as folder:
        staged = Path(folder) / "backup"
        with os.fdopen(os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if staged.read_bytes() != payload:
            raise OSError("Не удалось проверить резервную копию.")
        publish_new(staged, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return path


def _load(path):
    with Path(path).open("rb") as f:
        data = f.read(MAX_SIZE + 1)
    if not HEADER.size + 32 <= len(data) <= MAX_SIZE:
        raise ValueError("Неподдерживаемая резервная копия.")
    magic, model, count = HEADER.unpack_from(data)
    stride = 4 + 2 * BLOCK_SIZE
    if (magic != MAGIC or model not in MODELS or not 0 < count <= MAX_BLOCKS
            or len(data) != HEADER.size + count * stride + 32
            or not hmac.compare_digest(data[-32:], hashlib.sha256(data[:-32]).digest())):
        raise ValueError("Резервная копия повреждена или имеет неизвестный формат.")
    blocks = {}
    for offset in range(HEADER.size, len(data) - 32, stride):
        address, = struct.unpack_from("<I", data, offset)
        if address % BLOCK_SIZE or address in blocks:
            raise ValueError("Неверная таблица блоков резервной копии.")
        start = offset + 4
        blocks[address] = (data[start:start+BLOCK_SIZE], data[start+BLOCK_SIZE:start+2*BLOCK_SIZE])
    return model.decode("ascii"), blocks


def prepare_restore(path, *, helper=None):
    """Undo only recognizable, complete sector states. Never guess data after a partial erase."""
    from write_settings import read_baseline, proposal_from_blocks
    saved_model, saved = _load(path)
    model, binding, current = read_baseline(helper=helper)
    if model != saved_model or saved.keys() != current.keys():
        raise ValueError("Копия не соответствует модели или разметке подключённой рации.")
    sectors = {}
    for address in current:
        sectors.setdefault(address // SECTOR_SIZE, []).append(address)
    for addresses in sectors.values():
        if not any(all(current[a] == saved[a][state] for a in addresses) for state in (0, 1)):
            raise ValueError("Данные рации отличаются от копии. Автоматическое восстановление этого сектора невозможно.")
    original = {address: pair[0] for address, pair in saved.items()}
    return proposal_from_blocks(model, binding, current, original)
