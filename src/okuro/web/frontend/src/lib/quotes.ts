/**
 * Curated quote pool for PULSE ambient display.
 *
 * Two pools: 'contemplative' (shown when idle) and 'active' (during execution).
 * Rotates every ~45 seconds in the PULSE sidebar.
 */

export interface Quote {
  text: string;
  author: string;
  pool: "contemplative" | "active";
}

export const QUOTES: Quote[] = [
  // — Contemplative (idle) —
  { text: "The best way to predict the future is to invent it.", author: "Alan Kay", pool: "contemplative" },
  { text: "Simplicity is the ultimate sophistication.", author: "Leonardo da Vinci", pool: "contemplative" },
  { text: "We shape our tools, and thereafter our tools shape us.", author: "Marshall McLuhan", pool: "contemplative" },
  { text: "The purpose of a system is what it does.", author: "Stafford Beer", pool: "contemplative" },
  { text: "In the beginner's mind there are many possibilities, in the expert's mind there are few.", author: "Shunryu Suzuki", pool: "contemplative" },
  { text: "Everything should be made as simple as possible, but not simpler.", author: "Albert Einstein", pool: "contemplative" },
  { text: "The map is not the territory.", author: "Alfred Korzybski", pool: "contemplative" },
  { text: "Form follows function.", author: "Louis Sullivan", pool: "contemplative" },
  { text: "A complex system that works is invariably found to have evolved from a simple system that worked.", author: "John Gall", pool: "contemplative" },
  { text: "The only way to go fast is to go well.", author: "Robert C. Martin", pool: "contemplative" },
  { text: "Perfection is achieved not when there is nothing more to add, but when there is nothing left to take away.", author: "Antoine de Saint-Exupery", pool: "contemplative" },
  { text: "The limits of my language mean the limits of my world.", author: "Ludwig Wittgenstein", pool: "contemplative" },
  { text: "All models are wrong, but some are useful.", author: "George Box", pool: "contemplative" },
  { text: "The best time to plant a tree was 20 years ago. The second best time is now.", author: "Chinese proverb", pool: "contemplative" },
  { text: "Less, but better.", author: "Dieter Rams", pool: "contemplative" },
  { text: "Design is not just what it looks like. Design is how it works.", author: "Steve Jobs", pool: "contemplative" },
  { text: "The details are not the details. They make the design.", author: "Charles Eames", pool: "contemplative" },
  { text: "Any sufficiently advanced technology is indistinguishable from magic.", author: "Arthur C. Clarke", pool: "contemplative" },
  { text: "The medium is the message.", author: "Marshall McLuhan", pool: "contemplative" },
  { text: "There is nothing so useless as doing efficiently that which should not be done at all.", author: "Peter Drucker", pool: "contemplative" },
  { text: "Order is not pressure which is imposed on society from without, but an equilibrium which is set up from within.", author: "Jose Ortega y Gasset", pool: "contemplative" },
  { text: "The whole is greater than the sum of its parts.", author: "Aristotle", pool: "contemplative" },
  { text: "What I cannot create, I do not understand.", author: "Richard Feynman", pool: "contemplative" },
  { text: "The impediment to action advances action. What stands in the way becomes the way.", author: "Marcus Aurelius", pool: "contemplative" },
  { text: "Good design is as little design as possible.", author: "Dieter Rams", pool: "contemplative" },

  // Designers, architects, artists — systems, tools, craft.
  { text: "You never change things by fighting the existing reality. To change something, build a new model that makes the existing model obsolete.", author: "Buckminster Fuller", pool: "contemplative" },
  { text: "We are called to be architects of the future, not its victims.", author: "Buckminster Fuller", pool: "contemplative" },
  { text: "When I am working on a problem, I never think about beauty. But when I have finished, if the solution is not beautiful, I know it is wrong.", author: "Buckminster Fuller", pool: "contemplative" },
  { text: "A house is a machine for living in.", author: "Le Corbusier", pool: "contemplative" },
  { text: "Space and light and order. Those are the things that men need just as much as they need bread or a place to sleep.", author: "Le Corbusier", pool: "contemplative" },
  { text: "Even a brick wants to be something.", author: "Louis Kahn", pool: "contemplative" },
  { text: "Less is more.", author: "Mies van der Rohe", pool: "contemplative" },
  { text: "God is in the details.", author: "Mies van der Rohe", pool: "contemplative" },
  { text: "Complicating is easy, simplifying is difficult.", author: "Bruno Munari", pool: "contemplative" },
  { text: "Simplicity is not the goal. It is the by-product of a good idea and modest expectations.", author: "Paul Rand", pool: "contemplative" },
  { text: "Simplicity is not the absence of clutter; that is a consequence of simplicity.", author: "Jony Ive", pool: "contemplative" },
  { text: "Design dissolves in behavior.", author: "Naoto Fukasawa", pool: "contemplative" },
  { text: "Always design a thing by considering it in its next larger context — a chair in a room, a room in a house, a house in an environment, an environment in a city plan.", author: "Eero Saarinen", pool: "contemplative" },
  { text: "Simple is not easy.", author: "Josef Albers", pool: "contemplative" },
  { text: "Art is the concrete representation of our most subtle feelings.", author: "Agnes Martin", pool: "contemplative" },
  { text: "The essence of the independent mind lies not in what it thinks, but in how it thinks.", author: "Christopher Hitchens", pool: "contemplative" },
  { text: "Design is the silent ambassador of your brand.", author: "Paul Rand", pool: "contemplative" },
  { text: "Design is thinking made visual.", author: "Saul Bass", pool: "contemplative" },

  // — Active (during execution) —
  { text: "Move fast and fix things.", author: "okuro", pool: "active" },
  { text: "Working software over comprehensive documentation.", author: "Agile Manifesto", pool: "active" },
  { text: "Make it work, make it right, make it fast.", author: "Kent Beck", pool: "active" },
  { text: "Measure twice, cut once.", author: "proverb", pool: "active" },
  { text: "Programs must be written for people to read, and only incidentally for machines to execute.", author: "Abelson & Sussman", pool: "active" },
  { text: "Talk is cheap. Show me the code.", author: "Linus Torvalds", pool: "active" },
  { text: "The function of good software is to make the complex appear simple.", author: "Grady Booch", pool: "active" },
  { text: "First, solve the problem. Then, write the code.", author: "John Johnson", pool: "active" },
  { text: "Controlling complexity is the essence of computer programming.", author: "Brian Kernighan", pool: "active" },
  { text: "Code is like humor. When you have to explain it, it's bad.", author: "Cory House", pool: "active" },
  { text: "The art of programming is the art of organizing complexity.", author: "Edsger Dijkstra", pool: "active" },
  { text: "Before software can be reusable it first has to be usable.", author: "Ralph Johnson", pool: "active" },
  { text: "Any fool can write code that a computer can understand. Good programmers write code that humans can understand.", author: "Martin Fowler", pool: "active" },
  { text: "Debugging is twice as hard as writing the code in the first place.", author: "Brian Kernighan", pool: "active" },
  { text: "It's not a bug. It's an undocumented feature.", author: "anonymous", pool: "active" },
  { text: "The most dangerous phrase in the language is: We've always done it this way.", author: "Grace Hopper", pool: "active" },
  { text: "Weeks of coding can save you hours of planning.", author: "anonymous", pool: "active" },
  { text: "One of my most productive days was throwing away 1000 lines of code.", author: "Ken Thompson", pool: "active" },
  { text: "Premature optimization is the root of all evil.", author: "Donald Knuth", pool: "active" },
  { text: "Shipping beats perfection.", author: "okuro", pool: "active" },

  // Design + architecture thinkers on doing the work.
  { text: "Don't fight forces, use them.", author: "Buckminster Fuller", pool: "active" },
  { text: "Design depends largely on constraints.", author: "Charles Eames", pool: "active" },
  { text: "Good design is unobtrusive.", author: "Dieter Rams", pool: "active" },
  { text: "An architect's most useful tools are an eraser at the drafting board, and a wrecking bar at the site.", author: "Frank Lloyd Wright", pool: "active" },
];

const contemplative = QUOTES.filter((q) => q.pool === "contemplative");
const active = QUOTES.filter((q) => q.pool === "active");

/** Pick a random quote from the appropriate pool. */
export function pickQuote(isActive: boolean): Quote {
  const pool = isActive ? active : contemplative;
  return pool[Math.floor(Math.random() * pool.length)]!;
}
