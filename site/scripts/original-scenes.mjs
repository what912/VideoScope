function sourceFor(color, width, height, frameRate, duration) {
  return `color=c=0x${color}:s=${width}x${height}:r=${frameRate}:d=${duration}`;
}

function finishFor(outputWidth, outputHeight) {
  return `scale=${outputWidth}:${outputHeight}:flags=lanczos,format=yuv420p`;
}

function opticalApertureGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const radius = "hypot((X-W/2)/(W*0.44),(Y-H/2)/(H*0.44))";
  const outerRing = `exp(-pow(${radius}-0.72,2)*260)`;
  const innerRing = `exp(-pow(${radius}-0.45,2)*420)`;
  const orbit = `exp(-pow((X-W*(0.5+0.29*cos(${phase}*0.7)))/(W*0.035),2)-pow((Y-H*(0.5+0.23*sin(${phase}*0.7)))/(H*0.045),2))`;
  return [
    sourceFor("081117", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='8+20*${outerRing}+26*${innerRing}+38*${orbit}':g='17+128*${outerRing}+92*${innerRing}+210*${orbit}':b='23+142*${outerRing}+118*${innerRing}+220*${orbit}'`,
    `rotate=angle='0.008*sin(${motion}*0.8)':ow=iw:oh=ih:fillcolor=0x081117`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function observationGridGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const vertical = `lt(mod(X+36*sin(Y/H*2+${phase}*0.55),max(34,150-Y/10)),3)`;
  const horizontal = `lt(mod(Y+${phase}*26,72),3)`;
  const beaconA = `exp(-pow((X-W*0.31)/(W*0.018),2)-pow((Y-H*0.67)/(H*0.032),2))*(0.55+0.45*sin(${phase}*2.1))`;
  const beaconB = `exp(-pow((X-W*0.68)/(W*0.015),2)-pow((Y-H*0.49)/(H*0.026),2))*(0.55+0.45*sin(${phase}*2.1+2.1))`;
  const beaconC = `exp(-pow((X-W*0.52)/(W*0.012),2)-pow((Y-H*0.31)/(H*0.022),2))*(0.55+0.45*sin(${phase}*2.1+4.2))`;
  return [
    sourceFor("05070d", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='5+4*${vertical}+3*${horizontal}+16*(${beaconA}+${beaconB}+${beaconC})':g='7+70*${vertical}+40*${horizontal}+210*(${beaconA}+${beaconB}+${beaconC})':b='13+88*${vertical}+72*${horizontal}+190*(${beaconA}+${beaconB}+${beaconC})'`,
    `rotate=angle='0.012*sin(${motion}*0.35)':ow=iw:oh=ih:fillcolor=0x05070d`,
    "vignette=angle=PI/4",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function fluidSpectrumGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const bandA = `exp(-pow((Y-H*(0.27+0.055*sin(X/150+${phase}*0.7)))/(H*0.07),2))`;
  const bandB = `exp(-pow((Y-H*(0.51+0.07*sin(X/190-${phase}*0.52+1.4)))/(H*0.08),2))`;
  const bandC = `exp(-pow((Y-H*(0.72+0.045*sin(X/125+${phase}*0.9+2.8)))/(H*0.06),2))`;
  const interference = `(0.5+0.5*sin(X/24+Y/38+${phase}*1.3))`;
  return [
    sourceFor("071019", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='7+18*${bandA}+56*${bandB}+112*${bandC}*${interference}':g='16+180*${bandA}+132*${bandB}+58*${bandC}':b='25+172*${bandA}+236*${bandB}+215*${bandC}*${interference}'`,
    "gblur=sigma=1.4:steps=2",
    `rotate=angle='0.004*sin(${motion}*0.6)':ow=iw:oh=ih:fillcolor=0x071019`,
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function diagnosticMeshGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const meshX = `lt(mod(X+22*sin(Y/95+${phase}*0.55),96),3)`;
  const meshY = `lt(mod(Y+18*sin(X/120-${phase}*0.45),82),3)`;
  const reticleRing = `exp(-pow(hypot((X-W*0.67)/(W*0.12),(Y-H*0.43)/(H*0.2))-0.62,2)*900)`;
  const reticleCross = `between(X,W*0.655,W*0.685)*between(Y,H*0.27,H*0.59)+between(Y,H*0.415,H*0.445)*between(X,W*0.55,W*0.79)`;
  return [
    sourceFor("080b10", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='8+12*(${meshX}+${meshY})+225*${reticleRing}+92*${reticleCross}':g='11+128*(${meshX}+${meshY})+54*${reticleRing}+28*${reticleCross}':b='16+132*(${meshX}+${meshY})+55*${reticleRing}+34*${reticleCross}'`,
    `rotate=angle='0.006*sin(${motion}*0.44)':ow=iw:oh=ih:fillcolor=0x080b10`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function coolTopographyGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const terrain = `Y+82*sin(X/205+${phase}*0.42)+34*sin(X/73-${phase}*0.28)+18*sin(Y/91)`;
  const ridges = `lt(mod(${terrain},66),5)`;
  const ridgeGlow = `exp(-pow(mod(${terrain},66)-5,2)/68)`;
  return [
    sourceFor("06121a", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='6+11*${ridgeGlow}+13*${ridges}':g='18+82*${ridgeGlow}+188*${ridges}':b='26+96*${ridgeGlow}+171*${ridges}'`,
    `rotate=angle='0.003*sin(${motion}*0.5)':ow=iw:oh=ih:fillcolor=0x06121a`,
    "vignette=angle=PI/6",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function dawnSpectrumGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const horizon = `exp(-pow((Y-H*(0.64-0.035*sin(${phase}*0.45)))/(H*0.075),2))`;
  const sun = `exp(-pow((X-W*0.52)/(W*0.19),2)-pow((Y-H*(0.62-0.04*sin(${phase}*0.45)))/(H*0.18),2))`;
  const atmosphere = `(0.5+0.5*sin(Y/46-${phase}*0.9))*exp(-Y/(H*0.72))`;
  return [
    sourceFor("120a16", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='18+230*${horizon}+205*${sun}+34*${atmosphere}':g='10+128*${horizon}+118*${sun}+18*${atmosphere}':b='22+64*${horizon}+38*${sun}+48*${atmosphere}'`,
    "gblur=sigma=2.2:steps=2",
    `rotate=angle='0.002*sin(${motion}*0.3)':ow=iw:oh=ih:fillcolor=0x120a16`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function cyanCausticGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const arcA = `exp(-pow(hypot((X-W*0.23)/(W*0.55),(Y-H*0.58)/(H*0.72))-0.72-0.025*sin(${phase}),2)*1500)`;
  const arcB = `exp(-pow(hypot((X-W*0.76)/(W*0.48),(Y-H*0.39)/(H*0.62))-0.56-0.018*cos(${phase}*0.8),2)*1800)`;
  const arcC = `exp(-pow(hypot((X-W*0.46)/(W*0.65),(Y-H*0.82)/(H*0.7))-0.44,2)*2100)`;
  const negativeSpace = `1-exp(-pow((X-W*0.52)/(W*0.21),2)-pow((Y-H*0.49)/(H*0.25),2))`;
  return [
    sourceFor("041016", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='4+10*(${arcA}+${arcB})+8*${arcC}':g='16+178*(${arcA}+${arcB})*${negativeSpace}+112*${arcC}':b='22+196*(${arcA}+${arcB})*${negativeSpace}+155*${arcC}'`,
    "gblur=sigma=1.1:steps=2",
    `rotate=angle='0.003*sin(${motion}*0.5)':ow=iw:oh=ih:fillcolor=0x041016`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function violetLatticeGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const diagonalA = `lt(mod(X+Y+18*sin(${phase}),150),5)`;
  const diagonalB = `lt(mod(X-Y+24*cos(${phase}*0.7)+1500,150),5)`;
  const gap = `1-between(X,W*0.49,W*0.61)*between(Y,H*0.35,H*0.64)`;
  const fracture = `lt(abs(Y-H*0.5-0.24*(X-W*0.55)),7)*between(X,W*0.48,W*0.72)`;
  return [
    sourceFor("0b0714", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='11+88*(${diagonalA}+${diagonalB})*${gap}+158*${fracture}':g='7+64*(${diagonalA}+${diagonalB})*${gap}+112*${fracture}':b='20+190*(${diagonalA}+${diagonalB})*${gap}+235*${fracture}'`,
    `rotate=angle='0.004*sin(${motion}*0.4)':ow=iw:oh=ih:fillcolor=0x0b0714`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

function amberContourGraph({
  width, height, outputWidth, outputHeight, frameRate, duration, phase, motion,
}) {
  const radius = `hypot((X-W*(0.52+0.015*sin(${phase})))/(W*0.34),(Y-H*(0.52+0.012*cos(${phase})))/(H*0.42))`;
  const island = `exp(-pow(${radius},2)*2.8)`;
  const contourA = `exp(-pow(${radius}-0.56,2)*1200)`;
  const contourB = `exp(-pow(${radius}-0.82,2)*1450)`;
  const boundary = `exp(-pow(${radius}-1.02,2)*2300)`;
  return [
    sourceFor("150c08", width, height, frameRate, duration),
    "format=gbrp",
    `geq=r='21+142*${island}+178*${contourA}+205*${contourB}+235*${boundary}':g='12+68*${island}+84*${contourA}+113*${contourB}+158*${boundary}':b='8+22*${island}+24*${contourA}+34*${contourB}+62*${boundary}'`,
    `rotate=angle='0.002*sin(${motion}*0.35)':ow=iw:oh=ih:fillcolor=0x150c08`,
    "vignette=angle=PI/5",
    finishFor(outputWidth, outputHeight),
  ].join(",");
}

export const sceneDefinitions = Object.freeze({
  "optical-aperture": Object.freeze({
    palette: "081117-00d8e8-98fff4",
    signature: "radial-aperture-orbit",
    build: (context) => opticalApertureGraph(context),
  }),
  "night-observation-grid": Object.freeze({
    palette: "05070d-14566e-43d9c8",
    signature: "perspective-grid-pulse",
    build: (context) => observationGridGraph(context),
  }),
  "fluid-spectrum": Object.freeze({
    palette: "071019-21d4cf-7c4dff",
    signature: "laminar-spectrum-interference",
    build: (context) => fluidSpectrumGraph(context),
  }),
  "diagnostic-mesh": Object.freeze({
    palette: "080b10-17cbd1-ff6d6d",
    signature: "localized-mesh-reticle",
    build: (context) => diagnosticMeshGraph(context),
  }),
  "cool-topography": Object.freeze({
    palette: "06121a-167a8b-7af7df",
    signature: "layered-contour-parallax",
    build: (context) => coolTopographyGraph(context),
  }),
  "dawn-spectrum": Object.freeze({
    palette: "120a16-ff9c5a-f8d57a",
    signature: "horizon-atmosphere-rise",
    build: (context) => dawnSpectrumGraph(context),
  }),
  "cyan-caustic": Object.freeze({
    palette: "041016-05aebe-b8fff5",
    signature: "refracted-caustic-arcs",
    build: (context) => cyanCausticGraph(context),
  }),
  "violet-lattice": Object.freeze({
    palette: "0b0714-6147c7-c6a8ff",
    signature: "structural-lattice-discontinuity",
    build: (context) => violetLatticeGraph(context),
  }),
  "amber-contour": Object.freeze({
    palette: "150c08-b86424-ffd08a",
    signature: "diagnostic-contour-island",
    build: (context) => amberContourGraph(context),
  }),
});

function definitionFor(item) {
  const definition = sceneDefinitions[item.scene];
  if (definition === undefined) {
    throw new Error(`Unknown original media scene: ${item.scene}`);
  }
  return definition;
}

function contextFor(item, duration, phase, motion) {
  return {
    width: item.workingWidth,
    height: item.workingHeight,
    outputWidth: item.outputWidth,
    outputHeight: item.outputHeight,
    frameRate: item.frameRate,
    duration,
    phase,
    motion,
  };
}

export function videoFilterFor(item) {
  return definitionFor(item).build(
    contextFor(item, item.durationSeconds, "T", "t"),
  );
}

export function stillFilterFor(item) {
  return definitionFor(item).build(
    contextFor(
      item,
      1 / item.frameRate,
      String(item.stillTimeSeconds),
      String(item.stillTimeSeconds),
    ),
  );
}
