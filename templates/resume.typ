// jobscout packet resume (P6) — renders a selection plan.
// Data arrives via --input data=<path to packet-data.json>.
#let data = json(sys.inputs.data)

#set page(paper: "a4", margin: (x: 1.6cm, y: 1.3cm))
#set text(size: 10.5pt, lang: "en")
#set par(leading: 0.62em)

#let contact = data.at("contact", default: "")
#let links = data.at("links", default: ())

#place(top + right, text(size: 8.5pt, fill: rgb("666666"))[#links.join(" · ")])

#text(size: 17pt, weight: "bold")[#data.at("name", default: "")]
#v(-0.4em)
#text(size: 9pt, fill: rgb("555555"))[#contact]

#if data.at("summary", default: "") != "" [
  #v(0.6em)
  #data.summary
]

#if data.at("experience", default: ()) != () [
  #v(0.4em)
  == Experience
  #for exp in data.experience [
    #grid(
      columns: (auto, 1fr, auto),
      text(weight: "bold")[#exp.company],
      align(center)[#exp.title],
      align(right, text(size: 9pt, fill: rgb("666666"))[#exp.dates],
    )
    #v(0.15em)
    #for b in exp.bullets [
      #- #b
    ]
    #v(0.45em)
  ]
]

#if data.at("projects", default: ()) != () [
  == Projects
  #for proj in data.projects [
    #text(weight: "bold")[#proj.name]
    #v(0.15em)
    #for b in proj.bullets [
      #- #b
    ]
    #v(0.4em)
  ]
]

#if data.at("skills", default: ()) != () [
  == Skills
  #for s in data.skills [
    #text(weight: "bold")[#s.area:] #h(0.3em) #s.items.join(", ") \
  ]
]
