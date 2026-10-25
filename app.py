import math
import streamlit as st
import pandas as pd
from ortools.sat.python import cp_model

# ============================================================
# PAGE SETUP
# ============================================================
st.set_page_config(
    page_title="Nurse Roster Optimizer V4",
    page_icon="🏥",
    layout="wide"
)

st.markdown(
    """
    <style>
    h1, h2, h3 { color: #234E70; }
    [data-testid="stCheckbox"] div[data-baseweb="checkbox"] > div:first-child {
        background-color: #7BB7D9 !important;
        border-color: #7BB7D9 !important;
    }
    [data-testid="stCheckbox"] div[data-baseweb="checkbox"] svg {
        fill: white !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SHIFTS = ["Morning", "Evening", "Night"]
TARGET_SHIFTS_PER_TYPE = 2
MAX_NIGHT_SHIFTS_PER_WEEK = 2

if "roster" not in st.session_state:
    st.session_state["roster"] = None
if "roster_departments" not in st.session_state:
    st.session_state["roster_departments"] = {}

# ============================================================
# OPTIMIZER
# ============================================================
def generate_roster(nurses, availability_df, nurse_departments, department_staffing_requirements):
    """Create a department-specific weekly roster using CP-SAT."""
    model = cp_model.CpModel()
    x = {}

    for nurse in nurses:
        for di, day in enumerate(DAYS):
            for si, shift in enumerate(SHIFTS):
                x[nurse, di, si] = model.NewBoolVar(f"x_{nurses.index(nurse)}_{di}_{si}")

    # Availability, off days, shift-type limits, and valid same-day pairs.
    availability_lookup = {}
    for _, row in availability_df.iterrows():
        availability_lookup[(row["Nurse"], row["Day"])] = row

    for nurse in nurses:
        for di, day in enumerate(DAYS):
            row = availability_lookup[(nurse, day)]
            allowed = row["Available Shifts"]
            is_off = bool(row["Off day"])
            shift_type = row["Shift Type"]

            if is_off:
                for si in range(len(SHIFTS)):
                    model.Add(x[nurse, di, si] == 0)
                continue

            for si, shift in enumerate(SHIFTS):
                if shift not in allowed:
                    model.Add(x[nurse, di, si] == 0)

            # Single Shift (or no type selected) allows at most one shift.
            # Double Shift allows two consecutive shifts on the same day.
            daily_limit = 2 if shift_type == "Double Shift" else 1
            model.Add(sum(x[nurse, di, si] for si in range(3)) <= daily_limit)

            # Morning + Night are not consecutive on the same day.
            model.Add(x[nurse, di, 0] + x[nurse, di, 2] <= 1)

            # A same-day double must be Morning+Evening or Evening+Night.
            # The daily limit and the Morning+Night constraint enforce this.
        # Prevent consecutive-day duties when the next day is Single Shift.
    for nurse in nurses:
        for di in range(len(DAYS) - 1):
            next_day_row = availability_lookup[(nurse, DAYS[di + 1])]
            next_day_type = next_day_row["Shift Type"]

            if next_day_type == "Single Shift":
                model.Add(
                    sum(x[nurse, di, si] for si in range(len(SHIFTS)))
                    + sum(x[nurse, di + 1, si] for si in range(len(SHIFTS)))
                    <= 1
                )

    # Department-specific minimum staffing, for every day and shift.
    for department in ["ICU", "Ward"]:
        department_nurses = [n for n in nurses if nurse_departments.get(n) == department]
        req = department_staffing_requirements[department]
        for di, day in enumerate(DAYS):
            for si, shift in enumerate(SHIFTS):
                required = int(req[shift])
                model.Add(
                    sum(x[n, di, si] for n in department_nurses) >= required
                )

    # Weekly workload limit and no more than two Night shifts per nurse.
    for nurse in nurses:
        all_assignments = [
            x[nurse, di, si]
            for di in range(len(DAYS))
            for si in range(len(SHIFTS))
        ]

        model.Add(sum(x[nurse, di, 2] for di in range(len(DAYS))) <= MAX_NIGHT_SHIFTS_PER_WEEK)

    # Soft target: aim for two Morning, two Evening and two Night shifts
    # per nurse each week. Leave and staffing feasibility take priority.
    deviations = []
    for nurse in nurses:
        for si, shift in enumerate(SHIFTS):
            count = sum(x[nurse, di, si] for di in range(len(DAYS)))
            under = model.NewIntVar(0, 7, f"under_{nurses.index(nurse)}_{si}")
            over = model.NewIntVar(0, 7, f"over_{nurses.index(nurse)}_{si}")
            model.Add(count - TARGET_SHIFTS_PER_TYPE == over - under)
            deviations.extend([under, over])

    # Minimize deviation from the weekly shift targets. Staffing coverage
    # remains a hard constraint; no preference-shift feature is used.
    model.Minimize(sum(deviations))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 15
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status

    rows = []
    for nurse in nurses:
        row = {"Nurse": nurse, "Department": nurse_departments[nurse]}
        for di, day in enumerate(DAYS):
            assigned = [
                shift for si, shift in enumerate(SHIFTS)
                if solver.Value(x[nurse, di, si])
            ]
            row[day] = " + ".join(assigned) if assigned else "Off"
        rows.append(row)

    return pd.DataFrame(rows), status


def assigned_count(cell):
    if not isinstance(cell, str) or cell == "Off":
        return 0
    return sum(part in SHIFTS for part in cell.split(" + "))


def calculate_roster_metrics(roster_df, department_staffing_requirements):
    required = sum(
        int(department_staffing_requirements[dept][shift]) * 7
        for dept in ["ICU", "Ward"]
        for shift in SHIFTS
    )
    assigned = sum(
        assigned_count(roster_df.loc[i, day])
        for i in roster_df.index
        for day in DAYS
    )
    coverage = (assigned / required * 100) if required else 100
    return required, assigned, coverage


def explain_infeasibility(nurses, availability_df, nurse_departments, department_staffing_requirements):
    """Return practical, data-based reasons a roster may be infeasible."""
    issues = []
    availability_lookup = {
        (row["Nurse"], row["Day"]): row
        for _, row in availability_df.iterrows()
    }

    # Check whether enough nurses in each department are available for each shift.
    for department in ["ICU", "Ward"]:
        dept_nurses = [n for n in nurses if nurse_departments.get(n) == department]
        req = department_staffing_requirements[department]
        for day in DAYS:
            for shift in SHIFTS:
                eligible = 0
                for nurse in dept_nurses:
                    row = availability_lookup.get((nurse, day))
                    if row is not None and not bool(row["Off day"]) and shift in row["Available Shifts"]:
                        eligible += 1
                required = int(req[shift])
                if eligible < required:
                    issues.append(
                        f"{department}, {day} {shift}: {required} nurses are required, "
                        f"but only {eligible} assigned-to-{department} nurse(s) marked themselves available."
                    )

        weekly_demand = sum(int(req[shift]) * 7 for shift in SHIFTS)


    # Check for missing department assignments.
    unassigned = [n for n in nurses if nurse_departments.get(n) not in ("ICU", "Ward")]
    if unassigned:
        issues.append("These nurses do not have a department selected: " + ", ".join(unassigned) + ".")

    if not issues:
        issues.append(
            "No single obvious shortage was detected. The combination of Off Days, selected shift availability, "
            "Single / Double Shift settings and the maximum of 2 Night shifts may conflict."
        )

    suggestions = [
        "To make the roster feasible, first review the specific day/shift shortages above.",
        "Where operationally appropriate, mark additional nurses as available for the short shift, reduce that shift's staffing requirement, or add nurses to the affected department.",
        "Review multiple Off Days and shift selections. A nurse can only be assigned shifts they marked as available.",
        "Review department staffing requirements and nurse availability if the roster cannot be generated. Do not remove safety or rest policies without hospital approval."
    ]
    return issues, suggestions


def abbreviate_assignment(value):
    """Convert full shift names to compact roster labels."""
    if not isinstance(value, str) or value == "Off":
        return "Off"
    abbreviations = {"Morning": "M", "Evening": "E", "Night": "N"}
    return "+".join(abbreviations.get(part.strip(), part.strip()) for part in value.split("+"))


# ============================================================
# APPLICATION HEADING
# ============================================================
st.markdown(
    """
    <h1 style="text-align:center;font-size:36px;">
        Gurukrupa Multi Speciality Hospital
    </h1>
    <h2 style="text-align:center;font-size:24px;">
        Automated Nurse Roster and Workflow Optimization
    </h2>
    """,
    unsafe_allow_html=True
)

# ============================================================
# 1. STAFFING REQUIREMENTS
# ============================================================
st.header("1. Staffing Requirements")
st.caption("Default requirements are prefilled. Edit them if staffing needs change.")

icu_col, ward_col = st.columns(2)

with icu_col:
    st.subheader("ICU")
    icu_beds = st.number_input("Bed capacity", 1, 500, 8, key="icu_beds_gk")
    st.caption("Nurses required per shift")
    icu_morning = st.number_input("Morning", 0, 100, 2, key="icu_morning_gk")
    icu_evening = st.number_input("Evening", 0, 100, 2, key="icu_evening_gk")
    icu_night = st.number_input("Night", 0, 100, 2, key="icu_night_gk")

with ward_col:
    st.subheader("Ward")
    ward_beds = st.number_input("Bed capacity ", 1, 500, 24, key="ward_beds_gk")
    st.caption("Nurses required per shift")
    ward_morning = st.number_input("Morning ", 0, 100, 3, key="ward_morning_gk")
    ward_evening = st.number_input("Evening ", 0, 100, 3, key="ward_evening_gk")
    ward_night = st.number_input("Night ", 0, 100, 3, key="ward_night_gk")

department_staffing_requirements = {
    "ICU": {"Beds": int(icu_beds), "Morning": int(icu_morning), "Evening": int(icu_evening), "Night": int(icu_night)},
    "Ward": {"Beds": int(ward_beds), "Morning": int(ward_morning), "Evening": int(ward_evening), "Night": int(ward_night)}
}

weekly_required = sum(
    department_staffing_requirements[d][s] * 7
    for d in ["ICU", "Ward"] for s in SHIFTS
)

st.divider()
st.subheader("Weekly Staffing Summary")
c1, c2, c3 = st.columns(3)
c1.metric("ICU nurse-shifts", (icu_morning + icu_evening + icu_night) * 7)
c2.metric("Ward nurse-shifts", (ward_morning + ward_evening + ward_night) * 7)
c3.metric("Total nurse-shifts", weekly_required)

# ============================================================
# 2. NURSE AVAILABILITY
# ============================================================
def select_icu(i):
    if st.session_state.get(f"nurse_icu_gk_{i}", False):
        st.session_state[f"nurse_ward_gk_{i}"] = False

def select_ward(i):
    if st.session_state.get(f"nurse_ward_gk_{i}", False):
        st.session_state[f"nurse_icu_gk_{i}"] = False

st.header("2. Nurse Availability")
number_of_nurses = int(st.number_input(
    "Number of nurses", min_value=1, max_value=100, value=18, step=1,
    key="number_of_nurses_gurukrupa"
))

nurses = []
nurse_departments = {}
availability_records = []
missing_names = []
missing_departments = []

for i in range(number_of_nurses):
    serial_col, name_col, icu_col, ward_col = st.columns(
    [0.15, 1, 0.5, 0.5], gap="small"
)

    with serial_col:
        st.markdown(f"**{i + 1}.**")

    with name_col:
        nurse_name = st.text_input(
            f"Name of Nurse {i + 1}", placeholder="Enter nurse name",
            key=f"nurse_name_gk_{i}", label_visibility="collapsed"
        ).strip()
    icu_key, ward_key = f"nurse_icu_gk_{i}", f"nurse_ward_gk_{i}"
    if icu_key not in st.session_state:
        st.session_state[icu_key] = False
    if ward_key not in st.session_state:
        st.session_state[ward_key] = False
    with icu_col:
        st.checkbox("ICU", key=icu_key, on_change=select_icu, args=(i,))
    with ward_col:
        st.checkbox("Ward", key=ward_key, on_change=select_ward, args=(i,))

    if not nurse_name:
        missing_names.append(i + 1)
    else:
        nurses.append(nurse_name)
        if st.session_state[icu_key]:
            nurse_departments[nurse_name] = "ICU"
        elif st.session_state[ward_key]:
            nurse_departments[nurse_name] = "Ward"
        else:
            missing_departments.append(nurse_name)

    day_columns = st.columns(7)
    for di, day in enumerate(DAYS):
        with day_columns[di]:
            st.markdown(f"**{day[:3]}**")
            morning_key = f"morning_v5_{i}_{di}"
            evening_key = f"evening_v5_{i}_{di}"
            night_key = f"night_v5_{i}_{di}"
            off_key = f"off_v5_{i}_{di}"
            single_key = f"single_shift_v1_{i}_{di}"
            double_key = f"double_shift_v1_{i}_{di}"

            for key, default in [
                (morning_key, True), (evening_key, True), (night_key, True),
                (off_key, False), (single_key, True), (double_key, False)
            ]:
                if key not in st.session_state:
                    st.session_state[key] = default

            def handle_off_day(mk=morning_key, ek=evening_key, nk=night_key,
                               ok=off_key, sk=single_key, dk=double_key):
                if st.session_state[ok]:
                    st.session_state[mk] = False
                    st.session_state[ek] = False
                    st.session_state[nk] = False
                    st.session_state[sk] = False
                    st.session_state[dk] = False
                else:
                    st.session_state[mk] = True
                    st.session_state[ek] = True
                    st.session_state[nk] = True

            def handle_shift(mk=morning_key, ek=evening_key, nk=night_key, ok=off_key):
                if st.session_state[mk] or st.session_state[ek] or st.session_state[nk]:
                    st.session_state[ok] = False

            def handle_single(sk=single_key, dk=double_key):
                if st.session_state[sk]:
                    st.session_state[dk] = False

            def handle_double(sk=single_key, dk=double_key):
                if st.session_state[dk]:
                    st.session_state[sk] = False

            is_off = st.session_state[off_key]
            st.checkbox("Morning", key=morning_key, disabled=is_off, on_change=handle_shift)
            st.checkbox("Evening", key=evening_key, disabled=is_off, on_change=handle_shift)
            st.checkbox("Night", key=night_key, disabled=is_off, on_change=handle_shift)
            st.checkbox("Off Day", key=off_key, on_change=handle_off_day)
            st.checkbox("Single Shift", key=single_key, disabled=is_off, on_change=handle_single)
            st.checkbox("Double Shift", key=double_key, disabled=is_off, on_change=handle_double)

            selected_shifts = []
            if st.session_state[morning_key]:
                selected_shifts.append("Morning")
            if st.session_state[evening_key]:
                selected_shifts.append("Evening")
            if st.session_state[night_key]:
                selected_shifts.append("Night")
            if st.session_state[off_key]:
                selected_shifts = []

            availability_records.append({
                "Nurse": nurse_name or f"Nurse {i + 1}",
                "Day": day,
                "Available Shifts": selected_shifts,
                "Off day": bool(st.session_state[off_key]),
                "Shift Type": (
                    "Single Shift" if st.session_state[single_key]
                    else "Double Shift" if st.session_state[double_key]
                    else None
                )
            })
    st.divider()

availability_df = pd.DataFrame(availability_records)

# ============================================================
# 3. GENERATE ROSTER
# ============================================================
st.header("3. Generate Roster")
generate_button = st.button("Generate Weekly Roster", type="primary", use_container_width=True)

if generate_button:
    if len(missing_names) > 0:
        st.error("Please enter a name for every nurse.")
    elif len(nurses) != len(set(nurses)):
        st.error("Nurse names must be unique.")
    elif missing_departments:
        st.error("Please select ICU or Ward for: " + ", ".join(missing_departments))
    elif len(nurse_departments) != len(nurses):
        st.error("Please assign every nurse to ICU or Ward.")
    else:
        with st.spinner("Generating roster..."):
            roster_df, status = generate_roster(
                nurses, availability_df, nurse_departments,
                department_staffing_requirements
            )
        if roster_df is None:
            st.error("A feasible roster could not be generated with the current settings.")
            issues, suggestions = explain_infeasibility(
                nurses, availability_df, nurse_departments,
                department_staffing_requirements
            )
            st.markdown("**Likely reasons**")
            for issue in issues:
                st.markdown(f"- {issue}")
            st.markdown("**What you can change**")
            for suggestion in suggestions:
                st.markdown(f"- {suggestion}")
        else:
            st.session_state["roster"] = roster_df
            st.session_state["roster_departments"] = nurse_departments.copy()
            st.success("Weekly roster generated successfully.")

# ============================================================
# 4. ROSTER OUTPUT
# ============================================================
if st.session_state["roster"] is not None:
    roster_df = st.session_state["roster"]
    st.header("4. Weekly Roster")

    total_required, total_assigned, coverage = calculate_roster_metrics(
        roster_df, department_staffing_requirements
    )

    excess_assignments = max(0, total_assigned - total_required)

    summary_cols = st.columns(4)
    summary_cols[0].metric("Required nurse-shifts", total_required)
    summary_cols[1].metric("Assigned nurse-shifts", total_assigned)
    summary_cols[2].metric("Total shifts assigned", total_assigned)
    
    # Show compact roster in two separate department tables.
    # Each table has Nurse first, then Monday through Sunday.
    display_columns = ["Nurse"] + DAYS
    display_roster = roster_df.copy()
    for day in DAYS:
        display_roster[day] = display_roster[day].apply(abbreviate_assignment)

    st.subheader("ICU Roster")
    icu_roster = display_roster[display_roster["Department"] == "ICU"][display_columns]
    if icu_roster.empty:
        st.info("No nurses are currently assigned to ICU.")
    else:
        st.dataframe(icu_roster, use_container_width=True, hide_index=True)

    st.subheader("Ward Roster")
    ward_roster = display_roster[display_roster["Department"] == "Ward"][display_columns]
    if ward_roster.empty:
        st.info("No nurses are currently assigned to Ward.")
    else:
        st.dataframe(ward_roster, use_container_width=True, hide_index=True)

    st.subheader("Nurse Workload Summary")
    nurse_shift_counts = {
        row["Nurse"]: sum(assigned_count(row[day]) for day in DAYS)
        for _, row in roster_df.iterrows()
    }
    workload_df = pd.DataFrame([
        {
            "Nurse": nurse,
            "Department": roster_df.loc[roster_df["Nurse"] == nurse, "Department"].iloc[0],
            "Assigned Shifts": nurse_shift_counts.get(nurse, 0),
        }
        for nurse in roster_df["Nurse"].tolist()
    ]).sort_values(by=["Department", "Assigned Shifts", "Nurse"], ascending=[True, False, True])
    st.dataframe(workload_df, use_container_width=True, hide_index=True)

    st.subheader("Department Staffing Coverage")
    coverage_rows = []
    for dept in ["ICU", "Ward"]:
        dept_roster = roster_df[roster_df["Department"] == dept]
        for shift in SHIFTS:
            for day in DAYS:
                actual = sum(
                    1 for cell in dept_roster[day]
                    if shift in (cell.split(" + ") if isinstance(cell, str) else [])
                )
                required = int(department_staffing_requirements[dept][shift])
                coverage_rows.append({
                    "Department": dept, "Day": day, "Shift": shift,
                    "Required": required, "Assigned": actual,
                    "Status": "Met" if actual >= required else "Short"
                })
    coverage_df = pd.DataFrame(coverage_rows)
    st.dataframe(coverage_df, use_container_width=True, hide_index=True)

    if excess_assignments:
        st.info(f"The roster contains {excess_assignments} nurse-shifts above the minimum staffing requirement.")
    else:
        st.success("The roster meets the minimum staffing requirement without extra assignments.")

    # Export full shift names so the CSV remains clear for hospital use.
    st.download_button(
        label="Download Roster as CSV",
        data=roster_df[["Nurse", "Department"] + DAYS].to_csv(index=False),
        file_name="nurse_roster.csv",
        mime="text/csv"
    )

# ============================================================
# FOOTER
# ============================================================
st.divider()
st.caption("Nurse Roster Optimizer V4 | Python • OR-Tools CP-SAT • Streamlit")
