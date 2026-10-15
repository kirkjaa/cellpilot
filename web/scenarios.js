// Manufacturing jobs to record, for the AI Builder Cup track "Manufacturing: Intelligent Operations &
// Industrial Efficiency". Each is a job a small factory does by hand today. The operator card says what to
// act out, with household stand-ins for the parts; `brief` is what the study carries about the station and
// what the model is told. `expect` is our hypothesis for the recording; the study decides.

export const SCENARIOS = [
  {
    id: 'kitting',
    name: 'Kitting station',
    short: 'Parts from bins into a tray',
    where: 'Electronics and appliance assembly, job shops: one kit per product, made up from loose parts.',
    why: 'Kitting is repetitive and low-skill, with high turnover. It is usually the first job a factory asks to automate.',
    props: [
      'three bowls as bins A, B and C, each with one kind of part (bolts, washers, bottle caps, Lego bricks)',
      'a tray with compartments (egg carton, ice-cube tray, a box with dividers)',
      'a reference card that shows a complete kit',
      'small stickers as labels',
      'zip bags or small boxes, and an out pile',
    ],
    cycle: [
      'Take one part from bin A and put it in its slot in the tray.',
      'Take one part from bin B and put it in its slot.',
      'Take one part from bin C, turn it so its mark faces you, and seat it in its slot.',
      'Compare the kit with the reference card, then stick a label on the tray.',
      'Slide the kit into a bag, put it on the out pile, and pull the next empty tray in.',
    ],
    cameras: 'All three cameras see the bins, the tray and both hands for the whole cycle.',
    expect: 'Steps 1 and 2 automate; step 3 automate or needs a fixture; step 4 keep human; step 5 needs a fixture.',
    brief: 'A kitting station. The operator takes parts from three bins (A, B, C) and places one of each into the slots '
      + 'of a tray, orienting the third part, compares the kit with a reference card, applies a label, and puts the '
      + 'finished kit into a bag on the out pile.',
  },
  {
    id: 'machine-loading',
    name: 'Machine loading',
    short: 'Pick, orient, seat in a jig',
    where: 'Machining and moulding cells: loading a part into a jig for the next operation and taking the finished one out.',
    why: 'Machine tending is the most common first cobot job. A machine that waits for an operator loses output.',
    props: [
      'a bin of loose identical parts (dominoes, bolts, Lego bricks, pens)',
      'a jig: a box lid with the part’s outline drawn on it, or a tray with one slot',
      'a finished bin',
    ],
    cycle: [
      'Take the finished part out of the jig and drop it in the finished bin.',
      'Pick one loose part from the bin.',
      'Turn it to the marked orientation.',
      'Seat it in the jig outline and press it down.',
    ],
    cameras: 'The bin, the jig and both hands in every view; the jig outline visible from above in at least one view.',
    expect: 'Steps 1 and 4 automate; step 2 needs a fixture (a two-finger gripper picks poorly from a loose bin); step 3 automate or needs a fixture.',
    brief: 'A machine-loading station. From a bin of loose identical parts the operator picks one, turns it to a marked '
      + 'orientation and seats it in a jig outline, after moving the previously loaded part from the jig to a finished bin.',
  },
  {
    id: 'packing',
    name: 'Packing station',
    short: 'Items into a box, seal, label',
    where: 'Fulfilment, food and consumer goods: the last manual line before the goods ship.',
    why: 'Packing lines run on manual labour with high turnover. Sealing and labelling are the steps integrators quote first.',
    props: [
      'a stack of identical items (soap bars, tea boxes, phone cases)',
      'small boxes or zip bags',
      'insert cards (cut paper)',
      'tape, or the bag’s own zip',
      'address stickers',
      'a stack for finished parcels',
    ],
    cycle: [
      'Take a box or a bag and open it.',
      'Place two items in it.',
      'Add the insert card.',
      'Close it and seal it: tape, or press the zip.',
      'Apply the address label.',
      'Put the parcel on the finished stack.',
    ],
    cameras: 'The item stack, the box and both hands in every view; the sealing step needs a clear side view.',
    expect: 'Steps 2 and 6 automate; steps 1, 4 and 5 need a fixture; step 3 automate or needs a fixture.',
    brief: 'A packing station. The operator opens a box or bag, places two items and an insert card in it, closes and '
      + 'seals it, applies an address label and stacks the parcel.',
  },
  {
    id: 'assembly',
    name: 'Simple assembly',
    short: 'Two parts joined and tightened',
    where: 'Sub-assembly at small manufacturers: brackets, housings, plugs; two parts and a fastener.',
    why: 'Assembly is where “a robot can do it” is most often wrong: the fit and the tightening need a fixture or a hand.',
    props: [
      'a base part and a mating part that join (a jar and its lid, a bolt and a nut, a pen barrel and its cap, two Lego bricks)',
      'a holder for the base part (a cup, a box slot)',
      'a hand tool (screwdriver or Allen key), or two turns by hand',
      'a finished tray',
    ],
    cycle: [
      'Pick the base part and set it in the holder.',
      'Pick the mating part and fit it onto the base.',
      'Tighten it: two turns with the tool, or by hand.',
      'Lift it and look at the joint from two sides.',
      'Place the assembly in the finished tray.',
    ],
    cameras: 'The holder, the parts and both hands in every view; the tightening must be seen from the side.',
    expect: 'Steps 1 and 5 automate; step 2 needs a fixture; step 3 needs a fixture or keep human; step 4 keep human.',
    brief: 'An assembly station. The operator sets a base part in a holder, fits a mating part onto it, tightens the '
      + 'joint with a hand tool, inspects it, and places the assembly in a finished tray.',
  },
  {
    id: 'inspection',
    name: 'Inspection and sorting',
    short: 'Look, decide, pass or fail',
    where: 'Incoming inspection and final quality control, by eye under a lamp.',
    why: 'Shows the study saying no. The decision needs a person, and the tool should say so rather than promise a robot.',
    props: [
      'a tray of 20 to 30 identical parts, a few marked with a pen dot as defects',
      'a lamp',
      'a pass bin and a fail bin',
      'a tally sheet and a pen',
    ],
    cycle: [
      'Pick a part from the tray.',
      'Turn it under the lamp and look at every face.',
      'Drop it in the pass bin, or in the fail bin if it has a mark.',
      'Make a tick on the tally sheet.',
    ],
    cameras: 'The tray, both bins and the hands under the lamp in every view; one camera close enough to see the mark.',
    expect: 'Step 1 automate; steps 2 and 3 keep human; step 4 automate or needs a fixture.',
    brief: 'An inspection station. The operator picks each part from a tray, turns it under a lamp to look at every '
      + 'face, drops it into a pass or a fail bin, and marks a tally sheet.',
  },
];

export const RECORDING_RULES = [
  '8 to 10 complete cycles at the usual pace, without looking at the cameras.',
  'Keep the same hand for the same step in every cycle, so the steps line up.',
  'Only the bench in frame: no screens, keys, papers or the rest of the room.',
  'Written consent from the operator; the organiser may publish the video.',
];

export function scenarioById(id) {
  return SCENARIOS.find((s) => s.id === id) || null;
}
