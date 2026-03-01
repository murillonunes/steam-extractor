#!/usr/bin/env python3
"""
Steam Extractor - Extrai avaliações de jogos da Steam.
"""

import argparse
import requests
from pathlib import Path

def main():
    # Criar parser
    parser = argparse.ArgumentParser(
        description="Steam Extractor - Extrai avaliações de jogos da Steam",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  %(prog)s 1091500 pt-br
        """
    )

    # Argumentos obrigatórios
    parser.add_argument("appid", help="ID do jogo (ex: 1091500 para Cyberpunk 2077)")
    parser.add_argument("language", help="Idioma (ex: pt-br, en-us)")

    # Parse argumentos
    args = parser.parse_args()

    print("Steam Extractor v0.2.0")
    print(f"Jogo: {args.appid}")
    print(f"Idioma: {args.language}")
    print("Argumentos recebidos com sucesso!")

if __name__ == "__main__":
    main()