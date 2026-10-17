"""
Read every structure of an exit-kit compounds.sdf with RDKit, an independent
chemistry toolkit, and compare RDKit's InChIKey with the one CDD stored.

Run by `exit_kit.py --check-structures`; RDKit isn't a project dependency:

  uv run --no-project --with rdkit python check_structures.py compounds.sdf report.csv

Differences are expected for a small share of molecules: chemistry engines read
some stereo and isotope drawings differently, and exotic structures (e.g. metal
complexes) may not parse. They are listed in the report for a manual look.
"""

import csv
import sys
from collections import Counter

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")


def main():
    sdf_path, report_path = sys.argv[1], sys.argv[2]
    counts, rows = Counter(), []
    for mol in Chem.ForwardSDMolSupplier(sdf_path):
        counts["molecules"] += 1
        if mol is None:
            counts["unreadable by RDKit"] += 1
            rows.append(("?", "", "", "unreadable by RDKit"))
            continue
        cdd_id = (
            mol.GetProp("CDD_MOLECULE_ID") if mol.HasProp("CDD_MOLECULE_ID") else "?"
        )
        cdd_key = mol.GetProp("INCHIKEY") if mol.HasProp("INCHIKEY") else ""
        if not cdd_key:
            counts["no InChIKey from CDD"] += 1
            continue
        rdkit_key = Chem.MolToInchiKey(mol)
        if rdkit_key == cdd_key:
            counts["identical InChIKey"] += 1
        elif rdkit_key[:14] == cdd_key[:14]:
            counts["same skeleton, stereo/isotope layer differs"] += 1
            rows.append((cdd_id, cdd_key, rdkit_key, "stereo/isotope layer differs"))
        else:
            counts["different InChIKey"] += 1
            rows.append((cdd_id, cdd_key, rdkit_key, "different"))

    with open(report_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["cdd_molecule_id", "cdd_inchikey", "rdkit_inchikey", "problem"]
        )
        writer.writerows(rows)

    total = counts["molecules"] or 1
    for what, n in counts.items():
        print(
            f"  {what}: {n:,}" + ("" if what == "molecules" else f" ({n / total:.2%})")
        )
    print(f"  {len(rows):,} molecules to look at: {report_path}")


if __name__ == "__main__":
    main()
