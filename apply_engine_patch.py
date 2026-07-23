#!/usr/bin/env python3
from pathlib import Path
import sys

PATCH_ID = "PikaJieQi-7580820-RootGuard-LinuxLab-V1"

UCI_MARKER = '  o["MultiPV"] << Option(1, 1, 500);\n'
UCI_INSERT = '''  o["MultiPV"] << Option(1, 1, 500);\n\n  // Engine-native root tactical verifier.\n  o["Root Blunder Guard"] << Option(true);\n  o["Root Guard Candidates"] << Option(4, 2, 8);\n  o["Root Guard Hard Loss"] << Option(500, 100, 2000);\n  o["Root Guard Min Improvement"] << Option(180, 0, 1500);\n  o["Root Guard Max Score Drop"] << Option(150, 0, 1000);\n'''

NAMESPACE_MARKER = '} // namespace\n\n/// Search::init() is called at startup to initialize various lookup tables\n'
HELPER = r'''

struct RootGuardRisk {
  Value loss = VALUE_ZERO;
  Move reply = MOVE_NONE;
};

Value root_guard_piece_value(const Position& pos, Square sq) {
  const Piece pc = pos.piece_on(sq);
  if (pc == NO_PIECE)
    return VALUE_ZERO;
  if (type_of(pc) == KING)
    return VALUE_MATE;
  return PieceValue[MG][pc];
}

RootGuardRisk root_guard_immediate_risk(Position& pos, Move rootMove) {
  StateInfo st;
  RootGuardRisk worst;
  const bool reveals = pos.do_move(rootMove, st);

  auto scanCaptures = [&]() {
    for (const auto& ext : MoveList<LEGAL>(pos)) {
      const Move reply = ext;
      if (!pos.capture(reply))
        continue;
      const Square target = to_sq(reply);
      const Piece victim = pos.piece_on(target);
      if (victim == NO_PIECE)
        continue;
      const Value loss = root_guard_piece_value(pos, target);
      const bool kingLoss = type_of(victim) == KING;
      if ((kingLoss || pos.see_ge(reply, VALUE_ZERO)) && loss > worst.loss) {
        worst.loss = loss;
        worst.reply = reply;
      }
    }
  };

  if (reveals) {
    StateInfo darkSt;
    int typecount = 0;
    bool isDarkDepth = false;
    while (pos.getDark(darkSt, typecount, isDarkDepth)) {
      scanCaptures();
      pos.setDark();
    }
  } else {
    scanCaptures();
  }

  pos.undo_move(rootMove);
  return worst;
}

bool apply_root_blunder_guard(Position& pos, RootMoves& rootMoves) {
  if (!bool(Options["Root Blunder Guard"]) || rootMoves.size() < 2
      || rootMoves[0].pv.empty() || rootMoves[0].pv[0] == MOVE_NONE)
    return false;
  if (rootMoves[0].score >= VALUE_MATE_IN_MAX_PLY)
    return false;

  const Value hardLoss = Value(int(Options["Root Guard Hard Loss"]));
  const Value minImprovement = Value(int(Options["Root Guard Min Improvement"]));
  const Value maxScoreDrop = Value(int(Options["Root Guard Max Score Drop"]));
  const size_t candidates = std::min(rootMoves.size(), size_t(int(Options["Root Guard Candidates"])));

  const Value bestScore = rootMoves[0].score;
  const RootGuardRisk original = root_guard_immediate_risk(pos, rootMoves[0].pv[0]);
  if (original.loss < hardLoss)
    return false;

  size_t selected = 0;
  RootGuardRisk selectedRisk = original;
  for (size_t i = 1; i < candidates; ++i) {
    if (rootMoves[i].pv.empty() || rootMoves[i].pv[0] == MOVE_NONE
        || rootMoves[i].score == -VALUE_INFINITE)
      continue;
    if (bestScore - rootMoves[i].score > maxScoreDrop)
      continue;
    const RootGuardRisk candidate = root_guard_immediate_risk(pos, rootMoves[i].pv[0]);
    if (candidate.loss < selectedRisk.loss
        || (candidate.loss == selectedRisk.loss && rootMoves[i].score > rootMoves[selected].score)) {
      selected = i;
      selectedRisk = candidate;
    }
  }

  if (selected == 0 || original.loss - selectedRisk.loss < minImprovement) {
    sync_cout << "info string RootGuard keep " << UCI::move(rootMoves[0].pv[0])
              << " immediate_loss " << int(original.loss)
              << " reason no_better_candidate" << sync_endl;
    return false;
  }

  const Move originalMove = rootMoves[0].pv[0];
  const Move selectedMove = rootMoves[selected].pv[0];
  const Value scoreDrop = bestScore - rootMoves[selected].score;
  std::rotate(rootMoves.begin(), rootMoves.begin() + selected, rootMoves.begin() + selected + 1);
  sync_cout << "info string RootGuard veto " << UCI::move(originalMove)
            << " choose " << UCI::move(selectedMove)
            << " loss " << int(original.loss) << "->" << int(selectedRisk.loss)
            << " score_drop " << int(scoreDrop)
            << " reply " << (selectedRisk.reply == MOVE_NONE ? string("none") : UCI::move(selectedRisk.reply))
            << sync_endl;
  return true;
}
'''

MULTIPV_OLD = '''  size_t multiPV = size_t(Options["MultiPV"]);\n\n  multiPV = std::min(multiPV, rootMoves.size());\n'''
MULTIPV_NEW = '''  size_t multiPV = size_t(Options["MultiPV"]);\n\n  if (bool(Options["Root Blunder Guard"]))\n    multiPV = std::max(multiPV, size_t(int(Options["Root Guard Candidates"])));\n\n  multiPV = std::min(multiPV, rootMoves.size());\n'''

FINAL_OLD = '''  bestPreviousScore = bestThread->rootMoves[0].score;\n  bestPreviousAverageScore = bestThread->rootMoves[0].averageScore;\n'''
FINAL_NEW = '''  apply_root_blunder_guard(bestThread->rootPos, bestThread->rootMoves);\n\n  bestPreviousScore = bestThread->rootMoves[0].score;\n  bestPreviousAverageScore = bestThread->rootMoves[0].averageScore;\n'''

def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one marker, found {count}")
    return text.replace(old, new, 1)

def main() -> int:
    root = Path(sys.argv[1]).resolve()
    search = root / "src" / "search.cpp"
    uci = root / "src" / "ucioption.cpp"
    search_text = search.read_text(encoding="utf-8")
    uci_text = uci.read_text(encoding="utf-8")
    uci_text = replace_once(uci_text, UCI_MARKER, UCI_INSERT, "uci options")
    search_text = replace_once(search_text, NAMESPACE_MARKER,
        f"// {PATCH_ID}\n" + HELPER + "\n} // namespace\n\n/// Search::init() is called at startup to initialize various lookup tables\n",
        "helper insertion")
    search_text = replace_once(search_text, MULTIPV_OLD, MULTIPV_NEW, "multipv expansion")
    search_text = replace_once(search_text, FINAL_OLD, FINAL_NEW, "final root verification")
    search.write_text(search_text, encoding="utf-8")
    uci.write_text(uci_text, encoding="utf-8")
    print(f"Applied {PATCH_ID}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
