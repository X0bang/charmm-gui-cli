"""Small offline catalog, not a claim that every API module accepts each lipid.

Common phospholipid names checked against the official Individual Lipid Library:
https://www.charmm-gui.org/?doc=archive&lib=lipid (2026-10-13).
CHL1 is the cholesterol residue name used by existing verified build fixtures.
New/specialized server names require an explicit allow_unknown_lipids opt-in.
"""

KNOWN_LIPIDS = frozenset("""
CHL1
DLPA DMPA DPPA PSPA DSPA POPA PLPA SOPA SLPA DYPA YOPA DOPA DGPA DEPA DNPA
DDPC DCPC DLPC DMPC DPPC DSPC PSPC PYPC POPC PLPC PILPC PEPC SOPC SLPC DRPC
DYPC YOPC DOPC OEPC DUPC DGPC DEPC DNPC DLIPC
DLPE DMPE DPPE PSPE DSPE PYPE POPE PLPE PEPE SOPE SLPE DYPE YOPE OYPE DOPE
OEPE DGPE DEPE DNPE DLIPE MPPE
DLPG DMPG DPPG PSPG DSPG PYPG YPPG POPG OPPG PLPG SOPG SLPG DYPG DOPG DGPG DEPG DNPG
DLPS DMPS DPPS PSPS DSPS POPS PLPS PEPS SOPS SLPS OLPS DYPS YOPS DOPS OEPS DGPS DEPS DNPS
""".split())
