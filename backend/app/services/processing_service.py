from pathlib import Path
import subprocess


def run_rtklib_single(observation_file_path: str, output_dir: str) -> dict:
    obs_path = Path(observation_file_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    solution_path = out_dir / f"{obs_path.stem}.pos"

    # Ajuste depois para seu binário real
    # Exemplo esperado: rnx2rtkp
    command = [
        "/Users/pliniopotyguara/Downloads/RTKLIB/app/rnx2rtkp/gcc/rnx2rtkp",
        "-o",
        str(solution_path),
        str(obs_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr or "Falha no processamento RTKLIB.")

    return {
        "solution_path": str(solution_path),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
