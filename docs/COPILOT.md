# AI Racing Co-Pilot — Mathematical Specifications, Architecture & Tunables (CP-01..CP-13)

## 1. Operating Principle & Scope
The **UNBOUND AI Racing Co-Pilot** is a real-time, shared-autonomy gaming assistance architecture designed to stabilize high-speed vehicle control across somatic human interfaces (OmniBand V2 EMG, Optical Head/Face tracking, Hybrid, or Simulator).

The co-pilot does NOT drive autonomously; it provides **lateral path stabilization, cornering feedforward, adaptive cruise, and Automatic Emergency Braking (AEB)** while granting priority to human tactical decisions (overtaking, lane changes, nitro, drift).

---

## 2. Control Laws & Mathematics

### A. Lateral Controller (CP-04)
The lateral controller calculates requested front road wheel curvature $\kappa_{cmd}$:
$$\kappa_{cmd} = \kappa_{ff}(s + L) - K_e \cdot (e - e^*) - K_{\psi} \cdot \psi_e$$
where:
- $\kappa_{ff}(s + L)$: Analytical road curvature at dynamic lookahead distance $L$.
- $L = L_0 + k_v \cdot v$ ($L_0 = 6.0\text{ m}, k_v = 0.40\text{ s}$).
- $e$: Lateral displacement error from lane center (+right, metres).
- $e^*$: Human requested intra-lane bias offset ($\pm 1.5\text{ m}$).
- $\psi_e$: Heading angle error relative to road tangent vector (rad).
- Control gains: $K_e = 0.65\text{ m}^{-1}, K_{\psi} = 2.2\text{ rad}^{-1}$.

Requested road wheel angle:
$$\delta_{req} = \arctan(L_{wheelbase} \cdot \kappa_{cmd})$$
Normalized steering command:
$$u_{steer} = \text{clamp}\left(\frac{\delta_{req}}{\delta_{max}}, -1.0, 1.0\right)$$

### B. Longitudinal Controller & Cornering Guard (CP-05)
The target longitudinal speed $v^*$ is dynamically bounded by track geometry:
$$v^* = \min\left(v_{set}, \sqrt{\frac{a_{lat,max}}{\max(|\kappa_{ahead}|, 10^{-4})}}, v_{traffic}\right)$$
where $a_{lat,max} = 4.5\text{ m/s}^2$.

A Proportional-Integral (PI) speed tracker computes throttle and braking demands. Throttle and brake are **strictly mutually exclusive**:
$$\text{demand} = K_p (v^* - v) + K_i \int (v^* - v) dt$$
- $\text{demand} > 0.05 \implies \text{throttle} = \min(1.0, \text{demand}), \text{brake} = 0.0$.
- $\text{demand} < -0.05 \implies \text{brake} = \min(1.0, -\text{demand}), \text{throttle} = 0.0$.

---

## 3. First-Order Sigma-Delta Modulator (CP-06)
Continuous commands $u \in [-1.0, 1.0]$ are quantized into discrete digital keystrokes at the 60 Hz host engine tick:
$$\text{accumulator} \leftarrow \text{accumulator} + |u|$$
When $\text{accumulator} \ge 1.0$:
- Output digital KEY_DOWN in the active direction.
- $\text{accumulator} \leftarrow \text{accumulator} - 1.0$.

### Modulation Invariants:
1. **Mutual Exclusion:** `A` and `D` are NEVER simultaneously active. `W` and `S` are NEVER simultaneously active.
2. **Pulse Hygiene:** Minimum pulse and gap width of 2 ticks (33.3 ms) prevents game input buffering overflow.
3. Long-run duty cycle matches continuous input without fixed-period PWM latency.

---

## 4. Safety Layer & Transparent Vetoes (CP-08, CP-09)
1. **Forward AEB:**
   $$\text{TTC} = \frac{d_{obstacle}}{v_{closing}}$$
   - $\text{TTC} \le 1.2\text{ s} \implies$ Immediate full brake ($1.0$), cruise disengaged.
   - $\text{TTC} \le 2.0\text{ s} \implies$ Collision warning on HUD.
2. **Tactical Vetoes:**
   - **Nitro Vetoed:** When $|\kappa_{ahead}| \ge 0.025\text{ m}^{-1}$ or forward TTC $< 3.0\text{ s}$.
   - **Drift Vetoed:** When vehicle speed $< 12.0\text{ m/s}$ (prevents spinning out).
   - All vetoes emit visible HUD toasts and audit entries (`VETOED(reason)`).

