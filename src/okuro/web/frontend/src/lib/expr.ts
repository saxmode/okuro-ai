// <!-- AGENT_HEADER
// role: code
// purpose: A tiny SAFE arithmetic evaluator for the simulator block — shunting-
//   yard over + - * / % ^ ( ) with variables and unary minus. No eval / new
//   Function (no unsafe-eval reliance, no injection surface). Arithmetic only.
// index: tokenize | toRPN | evalRPN | evalExpr
// AGENT_HEADER_END -->

type Tok =
  | { t: "num"; v: number }
  | { t: "var"; v: string }
  | { t: "op"; v: string }
  | { t: "paren"; v: "(" | ")" };

const PREC: Record<string, number> = { "+": 1, "-": 1, "*": 2, "/": 2, "%": 2, "^": 3, "u-": 4 };
const RIGHT = new Set(["^", "u-"]); // right-associative

/** Split an expression into tokens. Unknown characters throw (→ NaN upstream). */
function tokenize(src: string): Tok[] {
  const toks: Tok[] = [];
  const s = src;
  let i = 0;
  while (i < s.length) {
    const c = s[i]!;
    if (c === " " || c === "\t" || c === "\n") { i++; continue; }
    if ((c >= "0" && c <= "9") || (c === "." && /[0-9]/.test(s[i + 1] ?? ""))) {
      let j = i + 1;
      while (j < s.length && /[0-9.]/.test(s[j]!)) j++;
      toks.push({ t: "num", v: parseFloat(s.slice(i, j)) });
      i = j; continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      let j = i + 1;
      while (j < s.length && /[A-Za-z0-9_]/.test(s[j]!)) j++;
      toks.push({ t: "var", v: s.slice(i, j) });
      i = j; continue;
    }
    if (c === "(" || c === ")") { toks.push({ t: "paren", v: c }); i++; continue; }
    if ("+-*/%^".includes(c)) { toks.push({ t: "op", v: c }); i++; continue; }
    throw new Error(`bad char '${c}'`);
  }
  return toks;
}

/** Shunting-yard → RPN, resolving unary minus to "u-". */
function toRPN(toks: Tok[]): Tok[] {
  const out: Tok[] = [];
  const stack: Tok[] = [];
  let prevValueLike = false; // after num/var/) → a following "-" is binary
  for (const tk of toks) {
    if (tk.t === "num" || tk.t === "var") { out.push(tk); prevValueLike = true; continue; }
    if (tk.t === "op") {
      const o1 = tk.v === "-" && !prevValueLike ? "u-" : tk.v;
      while (stack.length) {
        const top = stack[stack.length - 1]!;
        if (top.t !== "op") break;
        const o2 = top.v;
        if (RIGHT.has(o1) ? PREC[o1]! < PREC[o2]! : PREC[o1]! <= PREC[o2]!) out.push(stack.pop()!);
        else break;
      }
      stack.push({ t: "op", v: o1 });
      prevValueLike = false; continue;
    }
    if (tk.v === "(") { stack.push(tk); prevValueLike = false; continue; }
    // ")"
    while (stack.length && stack[stack.length - 1]!.t !== "paren") out.push(stack.pop()!);
    stack.pop(); // discard "("
    prevValueLike = true;
  }
  while (stack.length) {
    const top = stack.pop()!;
    if (top.t !== "paren") out.push(top); // unbalanced parens are tolerated
  }
  return out;
}

function evalRPN(rpn: Tok[], vars: Record<string, number>): number {
  const st: number[] = [];
  for (const tk of rpn) {
    if (tk.t === "num") { st.push(tk.v); continue; }
    if (tk.t === "var") { st.push(Number(vars[tk.v] ?? NaN)); continue; }
    if (tk.t !== "op") continue;
    if (tk.v === "u-") { st.push(-(st.pop() ?? NaN)); continue; }
    const b = st.pop() ?? NaN;
    const a = st.pop() ?? NaN;
    st.push(
      tk.v === "+" ? a + b : tk.v === "-" ? a - b : tk.v === "*" ? a * b :
      tk.v === "/" ? a / b : tk.v === "%" ? a % b : tk.v === "^" ? a ** b : NaN,
    );
  }
  return st.length ? st[st.length - 1]! : NaN;
}

/** Evaluate ``expr`` with ``vars``. Returns NaN on any failure — never throws
 *  into the render path. */
export function evalExpr(expr: string, vars: Record<string, number>): number {
  try {
    return evalRPN(toRPN(tokenize(expr)), vars);
  } catch {
    return NaN;
  }
}
