from pathlib import Path
import shutil
import subprocess
import re


def find_crx2rnx() -> str | None:
    for cmd in ["crx2rnx", "CRX2RNX"]:
        found = shutil.which(cmd)
        if found:
            return found
    return None


def _needs_hatanaka_conversion(source: Path) -> bool:
    lower = source.name.lower()

    if lower.endswith(".obs") or lower.endswith(".rnx"):
        return False

    if lower.endswith(".o"):
        return False

    if re.search(r"\.\d{2}o$", lower):
        return False

    if lower.endswith(".d"):
        return True

    if re.search(r"\.\d{2}d$", lower):
        return True

    return False


def _build_output_path(source: Path) -> Path:
    lower = source.name.lower()

    if lower.endswith(".d"):
        return source.with_name(source.name[:-1] + "o")

    match = re.search(r"\.(\d{2})d$", lower)
    if match:
        yy = match.group(1)
        return source.with_name(
            re.sub(rf"\.{yy}d$", f".{yy}o", source.name, flags=re.IGNORECASE)
        )

    return source.with_name(source.stem + ".o")


def convert_hatanaka_to_rinex(file_path: str) -> str:
    source = Path(file_path)

    if not source.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    if not _needs_hatanaka_conversion(source):
        print(f"[HATANAKA] Arquivo já legível, sem conversão: {source}")
        return str(source)

    converter = find_crx2rnx()
    print(f"[HATANAKA] Converter encontrado: {converter}")
    print(f"[HATANAKA] Arquivo fonte: {source}")

    if not converter:
        raise RuntimeError("crx2rnx não encontrado no sistema.")

    output_path = _build_output_path(source)

    if output_path.exists() and output_path.is_file():
        print(f"[HATANAKA] Arquivo convertido já existe, reutilizando: {output_path}")
        return str(output_path)

    result = subprocess.run(
        [converter, str(source)],
        cwd=str(source.parent),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )

    print(f"[HATANAKA] returncode: {result.returncode}")
    print(f"[HATANAKA] stdout: {result.stdout}")
    print(f"[HATANAKA] stderr: {result.stderr}")
    print(f"[HATANAKA] output esperado: {output_path}")

    if result.returncode != 0:
        raise RuntimeError(
            f"Falha ao converter arquivo Hatanaka: {result.stderr.strip() or result.stdout.strip()}"
        )

    if not output_path.exists():
        raise RuntimeError(f"Arquivo convertido não encontrado: {output_path}")

    print(f"[HATANAKA] Conversão concluída: {output_path}")
    return str(output_path)
