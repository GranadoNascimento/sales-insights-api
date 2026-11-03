"""Avalia o /sales-insights com o LLM de verdade usando eval/perguntas.json.

    python scripts/avaliar.py --url http://localhost:8000 --rodadas 3

Para comparar rodadas, suba a API com ANSWER_CACHE_TTL=0 (senão a 2a rodada vem do cache).
Sai com código 1 se a taxa de acerto ficar abaixo de --minimo.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def perguntar(url: str, pergunta: str, timeout: float) -> tuple[int, dict, float]:
    endereco = f"{url.rstrip('/')}/sales-insights?{urllib.parse.urlencode({'question': pergunta})}"
    inicio = time.perf_counter()
    try:
        with urllib.request.urlopen(endereco, timeout=timeout) as resp:
            status, corpo = resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            corpo = json.load(exc)
        except ValueError:
            corpo = {"detail": exc.reason}
    except (urllib.error.URLError, TimeoutError) as exc:
        status, corpo = 0, {"detail": f"sem resposta: {exc}"}
    return status, corpo, time.perf_counter() - inicio


def conferir(item: dict, status: int, corpo: dict, proibido: list[str]) -> bool:
    if status != item["status"]:
        return False
    if status != 200:
        return True
    resposta = corpo.get("answer", "").lower()
    if any(p.lower() in resposta for p in proibido):
        return False
    return all(any(opcao.lower() in resposta for opcao in trecho.split("|")) for trecho in item["esperado"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--rodadas", type=int, default=1)
    parser.add_argument("--arquivo", default=str(RAIZ / "eval" / "perguntas.json"))
    parser.add_argument("--saida", default=str(RAIZ / "resultado-avaliacao.json"))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--minimo", type=float, default=0.9, help="taxa mínima de acerto (0 a 1)")
    args = parser.parse_args()

    avaliacao = json.loads(Path(args.arquivo).read_text(encoding="utf-8"))
    perguntas = avaliacao["perguntas"]
    proibido = avaliacao.get("proibido", [])
    resultados = []
    respostas = defaultdict(set)
    veio_do_cache = False

    total = args.rodadas * len(perguntas)
    n = 0
    for rodada in range(1, args.rodadas + 1):
        for item in perguntas:
            n += 1
            status, corpo, segundos = perguntar(args.url, item["pergunta"], args.timeout)
            acertou = conferir(item, status, corpo, proibido)
            texto = corpo.get("answer") or corpo.get("detail") or ""
            veio_do_cache |= bool(corpo.get("cached"))
            respostas[item["pergunta"]].add(texto)
            resultados.append(
                {
                    "rodada": rodada,
                    "grupo": item.get("grupo", "geral"),
                    "pergunta": item["pergunta"],
                    "status": status,
                    "acertou": acertou,
                    "segundos": round(segundos, 1),
                    "resposta": texto,
                    "sql": corpo.get("sql"),
                }
            )
            marca = "OK " if acertou else "ERRO"
            print(f"[{n:>2}/{total}] {marca} {status} {segundos:6.1f}s  {item['pergunta']}")
            if not acertou:
                print(f"           resposta: {texto[:200]}")

    acertos = sum(r["acertou"] for r in resultados)
    taxa = acertos / len(resultados)
    llm = [r["segundos"] for r in resultados if r["status"] == 200]
    instaveis = [p for p, textos in respostas.items() if len(textos) > 1]

    print("\n=== Resumo ===")
    print(f"Acertos: {acertos}/{len(resultados)} ({taxa:.0%})")
    grupos = sorted({r["grupo"] for r in resultados})
    if len(grupos) > 1:
        for grupo in grupos:
            do_grupo = [r for r in resultados if r["grupo"] == grupo]
            certos = sum(r["acertou"] for r in do_grupo)
            print(f"  {grupo}: {certos}/{len(do_grupo)} ({certos / len(do_grupo):.0%})")
    if llm:
        print(f"Tempo médio com LLM: {sum(llm) / len(llm):.1f}s (máx. {max(llm):.1f}s)")
    if args.rodadas > 1:
        print(f"Perguntas com respostas diferentes entre rodadas: {len(instaveis)}")
        for p in instaveis:
            print(f"  - {p}")
    if veio_do_cache and args.rodadas > 1:
        print("Aviso: houve respostas do cache; para medir consistência use ANSWER_CACHE_TTL=0.")

    Path(args.saida).write_text(
        json.dumps(
            {"acertos": acertos, "total": len(resultados), "taxa": round(taxa, 3), "resultados": resultados},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Detalhes em {args.saida}")
    return 0 if taxa >= args.minimo else 1


if __name__ == "__main__":
    sys.exit(main())
