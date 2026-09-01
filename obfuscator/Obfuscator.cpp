//===- Obfuscator.cpp - pybin LLVM obfuscation pass-plugin ----------------===//
//
// An out-of-tree LLVM pass plugin (new pass manager) that obfuscates the C that
// Nuitka generates. Loads into a *stock* clang via `-fpass-plugin=` — no OLLVM
// fork or LLVM rebuild required. It runs at the OptimizerLast extension point so
// the regular optimizer cannot undo the transforms.
//
// Passes (each toggleable, see the cl::opt flags below):
//   * string/byte-constant encryption  (-obf-strings)  -- plugs `strings binary`
//   * MBA instruction substitution      (-obf-sub)
//   * bogus control flow                (-obf-bcf)
//   * control-flow flattening           (-obf-fla)
//
// Caveats this code cannot fix: obfuscation only covers Nuitka's generated C
// (not the CPython interpreter), in-memory state is recoverable at runtime, and
// naive flattening is undone by public deflatteners. It raises RE cost; it is
// not a wall. Build with ./build.sh and TEST the resulting binary.
//
//===----------------------------------------------------------------------===//

#include "llvm/IR/Constants.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Transforms/Utils/Local.h"        // DemoteRegToStack / DemotePHIToStack
#include "llvm/Transforms/Utils/ModuleUtils.h"  // appendToGlobalCtors

#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <set>
#include <vector>

using namespace llvm;

namespace {

// ---- configuration via environment ------------------------------------------
// cl::opt can't be used here: with `-fpass-plugin` clang parses `-mllvm` options
// *before* it dlopen's the plugin, so the plugin's options don't exist yet. The
// pass instead reads env vars at run time (pybin.py sets them in the build env):
//   PYBINOBF_STRINGS / _SUB / _BCF / _FLA   (1/0, default 1)
//   PYBINOBF_SUB_PROB / _BCF_PROB           (percent, default 40 / 30)
//   PYBINOBF_SEED                           (PRNG seed, default fixed)
bool envBool(const char *Name, bool Def) {
  const char *V = std::getenv(Name);
  if (!V || !*V)
    return Def;
  return !(std::strcmp(V, "0") == 0 || std::strcmp(V, "false") == 0 ||
           std::strcmp(V, "no") == 0);
}
unsigned envU(const char *Name, unsigned Def) {
  const char *V = std::getenv(Name);
  if (!V || !*V)
    return Def;
  char *End = nullptr;
  unsigned long R = std::strtoul(V, &End, 0);
  return (End && *End == '\0') ? static_cast<unsigned>(R) : Def;
}

// ---- PRNG -------------------------------------------------------------------
std::mt19937 &rng() {
  static std::mt19937 g(0x6f626675u);
  return g;
}
void seedRng(uint32_t S) { rng().seed(S); }
uint32_t rng32() { return rng()(); }
bool chance(unsigned pct) { return (rng()() % 100u) < pct; }

bool isOurHelper(const Function &F) { return F.getName().starts_with("__obf"); }

GlobalVariable *getInternalI32(Module &M, StringRef Name) {
  if (GlobalVariable *G = M.getNamedGlobal(Name))
    return G;
  Type *I32 = Type::getInt32Ty(M.getContext());
  return new GlobalVariable(M, I32, /*isConstant=*/false,
                            GlobalValue::InternalLinkage,
                            ConstantInt::get(I32, 0), Name);
}

//===----------------------------------------------------------------------===//
// String / byte-constant encryption
//===----------------------------------------------------------------------===//

// void __obf_decrypt(ptr p, i64 len, i8 k0, i8 k1):
//   for i in [0,len):  p[i] ^= k0 ^ (k1 + (i8)i)
Function *getDecryptFn(Module &M) {
  if (Function *F = M.getFunction("__obf_decrypt"))
    return F;
  LLVMContext &C = M.getContext();
  Type *VoidTy = Type::getVoidTy(C);
  PointerType *PtrTy = PointerType::getUnqual(C);
  Type *I64 = Type::getInt64Ty(C);
  Type *I8 = Type::getInt8Ty(C);

  FunctionType *FT = FunctionType::get(VoidTy, {PtrTy, I64, I8, I8}, false);
  Function *F =
      Function::Create(FT, GlobalValue::InternalLinkage, "__obf_decrypt", &M);
  F->setLinkage(GlobalValue::InternalLinkage);

  auto Arg = F->arg_begin();
  Value *P = &*Arg++;
  Value *Len = &*Arg++;
  Value *K0 = &*Arg++;
  Value *K1 = &*Arg++;

  BasicBlock *Entry = BasicBlock::Create(C, "entry", F);
  BasicBlock *Hdr = BasicBlock::Create(C, "hdr", F);
  BasicBlock *Body = BasicBlock::Create(C, "body", F);
  BasicBlock *End = BasicBlock::Create(C, "end", F);

  IRBuilder<> B(Entry);
  AllocaInst *IV = B.CreateAlloca(I64, nullptr, "i");
  B.CreateStore(ConstantInt::get(I64, 0), IV);
  B.CreateBr(Hdr);

  B.SetInsertPoint(Hdr);
  Value *I = B.CreateLoad(I64, IV, "i.v");
  Value *Cmp = B.CreateICmpULT(I, Len);
  B.CreateCondBr(Cmp, Body, End);

  B.SetInsertPoint(Body);
  Value *Idx = B.CreateLoad(I64, IV);
  Value *Ptr = B.CreateGEP(I8, P, Idx);
  Value *Ch = B.CreateLoad(I8, Ptr, "c");
  Value *It = B.CreateTrunc(Idx, I8);
  Value *Ks = B.CreateXor(K0, B.CreateAdd(K1, It));
  B.CreateStore(B.CreateXor(Ch, Ks), Ptr);
  B.CreateStore(B.CreateAdd(Idx, ConstantInt::get(I64, 1)), IV);
  B.CreateBr(Hdr);

  B.SetInsertPoint(End);
  B.CreateRetVoid();
  return F;
}

Function *getCtor(Module &M) {
  if (Function *F = M.getFunction("__obf_ctor"))
    return F;
  LLVMContext &C = M.getContext();
  FunctionType *FT = FunctionType::get(Type::getVoidTy(C), false);
  Function *F =
      Function::Create(FT, GlobalValue::InternalLinkage, "__obf_ctor", &M);
  BasicBlock *BB = BasicBlock::Create(C, "entry", F);
  IRBuilder<>(BB).CreateRetVoid();
  appendToGlobalCtors(M, F, /*Priority=*/0);
  return F;
}

bool encryptStrings(Module &M, size_t MaxBytes) {
  std::vector<GlobalVariable *> Targets;
  for (GlobalVariable &GV : M.globals()) {
    if (!GV.hasInitializer() || !GV.hasLocalLinkage())
      continue; // local linkage only -> each TU's ctor decrypts its own data
    if (GV.isThreadLocal() || GV.hasSection())
      continue;
    if (GV.getName().starts_with("llvm.") || GV.getName().starts_with("__obf"))
      continue;
    auto *CDA = dyn_cast<ConstantDataArray>(GV.getInitializer());
    if (!CDA || !CDA->getElementType()->isIntegerTy(8) ||
        CDA->getNumElements() == 0)
      continue; // i8 arrays (C strings / byte blobs) only
    if (MaxBytes && CDA->getNumElements() > MaxBytes)
      continue; // skip huge blobs (e.g. Nuitka's constants table): the in-place
                // startup decrypt and .data growth outweigh the benefit
    Targets.push_back(&GV);
  }
  if (Targets.empty())
    return false;

  LLVMContext &C = M.getContext();
  Function *Dec = getDecryptFn(M);
  Function *Ctor = getCtor(M);
  Instruction *Ret = Ctor->getEntryBlock().getTerminator();

  for (GlobalVariable *GV : Targets) {
    auto *CDA = cast<ConstantDataArray>(GV->getInitializer());
    StringRef Raw = CDA->getRawDataValues();
    uint8_t K0 = static_cast<uint8_t>(rng32());
    uint8_t K1 = static_cast<uint8_t>(rng32());

    std::vector<uint8_t> Enc(Raw.size());
    for (size_t i = 0; i < Raw.size(); ++i) {
      uint8_t Ks = static_cast<uint8_t>(K0 ^ static_cast<uint8_t>(K1 + static_cast<uint8_t>(i)));
      Enc[i] = static_cast<uint8_t>(Raw[i]) ^ Ks;
    }

    GV->setInitializer(ConstantDataArray::get(C, ArrayRef<uint8_t>(Enc)));
    GV->setConstant(false); // must live in writable memory for in-place decrypt

    IRBuilder<> B(Ret);
    B.CreateCall(Dec, {GV, ConstantInt::get(Type::getInt64Ty(C), Raw.size()),
                       ConstantInt::get(Type::getInt8Ty(C), K0),
                       ConstantInt::get(Type::getInt8Ty(C), K1)});
  }
  return true;
}

//===----------------------------------------------------------------------===//
// MBA instruction substitution
//===----------------------------------------------------------------------===//

bool substitute(Function &F, unsigned Prob) {
  std::vector<BinaryOperator *> Ops;
  for (BasicBlock &BB : F)
    for (Instruction &I : BB)
      if (auto *BO = dyn_cast<BinaryOperator>(&I))
        // Require >= 8 bits: for i1 the Add identity's "<< 1" is a
        // shift-by-bitwidth, which is poison in LLVM. Narrow ints are negligible.
        if (BO->getType()->isIntegerTy() &&
            BO->getType()->getIntegerBitWidth() >= 8)
          Ops.push_back(BO);

  bool Changed = false;
  for (BinaryOperator *BO : Ops) {
    if (!chance(Prob))
      continue;
    IRBuilder<> B(BO);
    Value *A = BO->getOperand(0), *D = BO->getOperand(1), *R = nullptr;
    switch (BO->getOpcode()) {
    case Instruction::Add: // a+b = (a^b) + ((a&b)<<1)
      R = B.CreateAdd(B.CreateXor(A, D), B.CreateShl(B.CreateAnd(A, D), 1));
      break;
    case Instruction::Sub: // a-b = a + ~b + 1
      R = B.CreateAdd(B.CreateAdd(A, B.CreateNot(D)),
                      ConstantInt::get(BO->getType(), 1));
      break;
    case Instruction::And: // a&b = (a+b) - (a|b)
      R = B.CreateSub(B.CreateAdd(A, D), B.CreateOr(A, D));
      break;
    case Instruction::Or: // a|b = (a&b) + (a^b)
      R = B.CreateAdd(B.CreateAnd(A, D), B.CreateXor(A, D));
      break;
    case Instruction::Xor: // a^b = (a|b) - (a&b)
      R = B.CreateSub(B.CreateOr(A, D), B.CreateAnd(A, D));
      break;
    default:
      continue;
    }
    BO->replaceAllUsesWith(R);
    BO->eraseFromParent();
    Changed = true;
  }
  return Changed;
}

//===----------------------------------------------------------------------===//
// Bogus control flow (always-true opaque predicate; both arms reach the real
// code, so the transform is correct regardless of how the predicate folds).
//===----------------------------------------------------------------------===//

bool bogus(Function &F, unsigned Prob) {
  Module &M = *F.getParent();
  LLVMContext &C = M.getContext();
  Type *I32 = Type::getInt32Ty(C);
  GlobalVariable *Opq = getInternalI32(M, "__obf_opq");
  GlobalVariable *Sink = getInternalI32(M, "__obf_sink");
  BasicBlock *Entry = &F.getEntryBlock();

  std::vector<BasicBlock *> Blocks;
  for (BasicBlock &BB : F)
    Blocks.push_back(&BB);

  bool Changed = false;
  for (BasicBlock *BB : Blocks) {
    if (BB == Entry || BB->isEHPad() || BB->size() < 2)
      continue; // keep entry's allocas in entry (flattening relies on it)
    if (!chance(Prob))
      continue;
    BasicBlock::iterator SP = BB->getFirstInsertionPt();
    if (SP == BB->end())
      continue;

    BasicBlock *Tail = BB->splitBasicBlock(SP, BB->getName() + ".real");
    Instruction *Uncond = BB->getTerminator(); // br Tail (created by split)

    // opaque-true: ((x*(x+1)) & 1) == 0   (x*(x+1) is always even)
    IRBuilder<> B(Uncond);
    Value *X = B.CreateLoad(I32, Opq, /*isVolatile=*/true, "opq");
    Value *Prod = B.CreateMul(X, B.CreateAdd(X, ConstantInt::get(I32, 1)));
    Value *Pred = B.CreateICmpEQ(B.CreateAnd(Prod, ConstantInt::get(I32, 1)),
                                 ConstantInt::get(I32, 0));

    BasicBlock *Bog = BasicBlock::Create(C, BB->getName() + ".bog", &F, Tail);
    B.CreateCondBr(Pred, Tail, Bog);
    Uncond->eraseFromParent();

    IRBuilder<> BB2(Bog);
    Value *J = BB2.CreateLoad(I32, Opq, /*isVolatile=*/true);
    BB2.CreateStore(BB2.CreateMul(J, ConstantInt::get(I32, 0x1000193)), Sink,
                    /*isVolatile=*/true);
    BB2.CreateBr(Tail);
    Changed = true;
  }
  return Changed;
}

//===----------------------------------------------------------------------===//
// Control-flow flattening (OLLVM-style dispatcher).
//===----------------------------------------------------------------------===//

bool flatten(Function &F, unsigned MaxBlocks) {
  // Only handle simple CFGs: br/ret/unreachable terminators, no EH, no indirect
  // control flow, and allocas confined to the entry block. Bail otherwise.
  for (BasicBlock &BB : F) {
    if (BB.isEHPad())
      return false;
    for (Instruction &I : BB)
      if (isa<InvokeInst>(I) || isa<CallBrInst>(I) || isa<IndirectBrInst>(I) ||
          isa<ResumeInst>(I) || isa<CatchSwitchInst>(I) ||
          isa<CatchReturnInst>(I) || isa<CleanupReturnInst>(I))
        return false;
    if (isa<SwitchInst>(BB.getTerminator()))
      return false;
  }
  BasicBlock *Entry = &F.getEntryBlock();
  for (BasicBlock &BB : F)
    if (&BB != Entry)
      for (Instruction &I : BB)
        if (isa<AllocaInst>(I))
          return false;

  // Make the entry a clean prologue ending in a single unconditional branch.
  if (Entry->getTerminator()->getNumSuccessors() > 1)
    Entry->splitBasicBlock(Entry->getTerminator()->getIterator(), "entry.split");
  if (Entry->getTerminator()->getNumSuccessors() != 1)
    return false;

  std::vector<BasicBlock *> Blocks;
  for (BasicBlock &BB : F)
    if (&BB != Entry)
      Blocks.push_back(&BB);
  if (Blocks.size() < 2)
    return false;
  if (MaxBlocks && Blocks.size() > MaxBlocks)
    return false; // avoid pathological compile time on huge generated functions

  // Break SSA values that cross blocks so the dispatcher rewrite stays valid.
  std::vector<PHINode *> PHIs;
  for (BasicBlock *BB : Blocks)
    for (Instruction &I : *BB)
      if (auto *P = dyn_cast<PHINode>(&I))
        PHIs.push_back(P);
  for (PHINode *P : PHIs)
    DemotePHIToStack(P);

  std::vector<Instruction *> ToDemote;
  for (BasicBlock &BB : F)
    for (Instruction &I : BB)
      if (!isa<AllocaInst>(I) && I.isUsedOutsideOfBlock(&BB))
        ToDemote.push_back(&I);
  for (Instruction *I : ToDemote)
    DemoteRegToStack(*I);

  LLVMContext &C = F.getContext();
  Type *I32 = Type::getInt32Ty(C);

  // Assign a distinct random state id to each block.
  std::map<BasicBlock *, uint32_t> State;
  std::set<uint32_t> Used;
  for (BasicBlock *BB : Blocks) {
    uint32_t S;
    do {
      S = rng32();
    } while (!Used.insert(S).second);
    State[BB] = S;
  }

  // switch variable in the entry (allocated once).
  IRBuilder<> EB(&*Entry->getFirstInsertionPt());
  AllocaInst *SwVar = EB.CreateAlloca(I32, nullptr, "obf.sw");

  BasicBlock *FirstBB = Entry->getTerminator()->getSuccessor(0);

  BasicBlock *Dispatch = BasicBlock::Create(C, "obf.dispatch", &F);
  BasicBlock *Dflt = BasicBlock::Create(C, "obf.default", &F);
  BasicBlock *LoopEnd = BasicBlock::Create(C, "obf.loopend", &F);

  // entry: store initial state, jump into the dispatch loop.
  Instruction *OldTerm = Entry->getTerminator();
  IRBuilder<> B(OldTerm);
  B.CreateStore(ConstantInt::get(I32, State[FirstBB]), SwVar);
  OldTerm->eraseFromParent();
  IRBuilder<>(Entry).CreateBr(Dispatch);

  B.SetInsertPoint(Dispatch);
  LoadInst *SwVal = B.CreateLoad(I32, SwVar, "obf.state");
  SwitchInst *Sw = B.CreateSwitch(SwVal, Dflt, Blocks.size());
  IRBuilder<>(Dflt).CreateBr(LoopEnd);
  IRBuilder<>(LoopEnd).CreateBr(Dispatch);

  for (BasicBlock *BB : Blocks) {
    BB->moveBefore(LoopEnd);
    Sw->addCase(ConstantInt::get(cast<IntegerType>(I32), State[BB]), BB);
  }

  // Rewire each block's terminator to set the next state and loop back.
  for (BasicBlock *BB : Blocks) {
    Instruction *Term = BB->getTerminator();
    if (isa<ReturnInst>(Term) || isa<UnreachableInst>(Term))
      continue;
    auto *Br = dyn_cast<BranchInst>(Term);
    if (!Br)
      continue;
    IRBuilder<> TB(Term);
    if (Br->isUnconditional()) {
      TB.CreateStore(ConstantInt::get(I32, State[Br->getSuccessor(0)]), SwVar);
    } else {
      Value *Next = TB.CreateSelect(
          Br->getCondition(), ConstantInt::get(I32, State[Br->getSuccessor(0)]),
          ConstantInt::get(I32, State[Br->getSuccessor(1)]));
      TB.CreateStore(Next, SwVar);
    }
    TB.CreateBr(LoopEnd);
    Term->eraseFromParent();
  }
  return true;
}

//===----------------------------------------------------------------------===//
// Module pass driver
//===----------------------------------------------------------------------===//

struct ObfuscatorPass : PassInfoMixin<ObfuscatorPass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &) {
    seedRng(envU("PYBINOBF_SEED", 0x6f626675u));
    const bool DoStrings = envBool("PYBINOBF_STRINGS", true);
    const bool DoSub = envBool("PYBINOBF_SUB", true);
    const bool DoBcf = envBool("PYBINOBF_BCF", true);
    const bool DoFla = envBool("PYBINOBF_FLA", true);
    const unsigned SubP = envU("PYBINOBF_SUB_PROB", 40);
    const unsigned BcfP = envU("PYBINOBF_BCF_PROB", 30);
    const unsigned FlaMax = envU("PYBINOBF_FLA_MAX_BLOCKS", 1500);
    const unsigned StrMax = envU("PYBINOBF_STR_MAX_BYTES", 0); // 0 = unlimited

    bool Changed = false;
    if (DoStrings)
      Changed |= encryptStrings(M, StrMax);

    for (Function &F : M) {
      if (F.isDeclaration() || isOurHelper(F))
        continue;
      if (DoSub)
        Changed |= substitute(F, SubP);
      if (DoBcf)
        Changed |= bogus(F, BcfP);
      if (DoFla)
        Changed |= flatten(F, FlaMax);
    }

    if (Changed && verifyModule(M, &errs()))
      report_fatal_error("pybinobf: transform produced invalid IR (please file a bug)");

    return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }

  static bool isRequired() { return true; }
};

} // namespace

//===----------------------------------------------------------------------===//
// Plugin registration
//===----------------------------------------------------------------------===//

llvm::PassPluginLibraryInfo getPybinObfPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "pybinobf", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            // Run last, after the optimizer, so it can't undo our work.
            // The OptimizerLast callback gained a ThinOrFullLTOPhase parameter in
            // LLVM 20; keep the plugin buildable on LLVM 17-21.
            PB.registerOptimizerLastEPCallback(
#if LLVM_VERSION_MAJOR >= 20
                [](ModulePassManager &MPM, OptimizationLevel, ThinOrFullLTOPhase) {
#else
                [](ModulePassManager &MPM, OptimizationLevel) {
#endif
                  MPM.addPass(ObfuscatorPass());
                });
            // Also allow explicit `-passes=pybinobf` invocation (e.g. via opt).
            PB.registerPipelineParsingCallback(
                [](StringRef Name, ModulePassManager &MPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "pybinobf") {
                    MPM.addPass(ObfuscatorPass());
                    return true;
                  }
                  return false;
                });
          }};
}

extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return getPybinObfPluginInfo();
}
