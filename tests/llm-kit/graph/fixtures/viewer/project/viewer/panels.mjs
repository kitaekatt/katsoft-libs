// A sample project panel: a one-line verdict for any node whose output has a score.
export default [
  {
    id: "verdict",
    title: "Verdict",
    applies: (node) => node.out && typeof node.out.score === "number",
    render: (node, api) => api.el("p", {}, `Score ${node.out.score} out of 10`,
      node.out.notes ? api.el("span", { class: "muted" }, ` - ${node.out.notes}`) : null),
  },
];
