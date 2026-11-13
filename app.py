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
    """Create a weekly roster allowing nurses to work in ICU, Ward, or both."""

    model = cp_model.CpModel()
    departments = ["ICU", "Ward"]
    x = {}
    workday = {}

    # Create assignment variables.
    for nurse in nurses:
        for department in departments:
            for di, day in enumerate(DAYS):
                for si, shift in enumerate(SHIFTS):
                    x[nurse, department, di, si] = model.NewBoolVar(
                        f"x_{nurses.index(nurse)}_{department}_{di}_{si}"
                    )

    availability_lookup = {
        (row["Nurse"], row["Day"]): row
        for _, row in availability_df.iterrows()
    }

    # --------------------------------------------------------
    # AVAILABILITY AND SHIFT CONSTRAINTS
    # --------------------------------------------------------

    for nurse in nurses:
        eligible_departments = nurse_departments.get(nurse, [])

        for di, day in enumerate(DAYS):
            row = availability_lookup[(nurse, day)]
            allowed = row["Available Shifts"]
            is_off = bool(row["Off day"])
            shift_type = row["Shift Type"]

            daily_assignments = []

            for department in departments:
                for si, shift in enumerate(SHIFTS):
                    variable = x[nurse, department, di, si]
                    daily_assignments.append(variable)

                    if department not in eligible_departments:
                        model.Add(variable == 0)

                    if is_off or shift not in allowed:
                        model.Add(variable == 0)

            # At most one assignment of each shift across departments.
            for si in range(len(SHIFTS)):
                model.Add(
                    sum(x[nurse, dept, di, si] for dept in departments) <= 1
                )

            # Single Shift = maximum one shift per day.
            # Double Shift = maximum two shifts per day.
            daily_limit = 2 if shift_type == "Double Shift" else 1
            model.Add(sum(daily_assignments) <= daily_limit)

            # Morning + Night is not a permitted double shift.
            model.Add(
                sum(x[nurse, dept, di, 0] for dept in departments)
                + sum(x[nurse, dept, di, 2] for dept in departments)
                <= 1
            )

            # Track whether the nurse works on this day.
            workday[nurse, di] = model.NewBoolVar(
                f"workday_{nurses.index(nurse)}_{di}"
            )

            model.Add(sum(daily_assignments) <= 2 * workday[nurse, di])
            model.Add(sum(daily_assignments) >= workday[nurse, di])

    # --------------------------------------------------------
    # PREFER A GAP BETWEEN CONSECUTIVE SINGLE SHIFTS
    # --------------------------------------------------------

    single_shift_consecutive_penalties = []

    for nurse in nurses:
        for di in range(len(DAYS) - 1):
            current_row = availability_lookup[(nurse, DAYS[di])]
            next_row = availability_lookup[(nurse, DAYS[di + 1])]

            if (
                current_row["Shift Type"] == "Single Shift"
                or next_row["Shift Type"] == "Single Shift"
            ):
                penalty = model.NewBoolVar(
                    f"consecutive_single_{nurses.index(nurse)}_{di}"
                )

                model.Add(
                    penalty >= workday[nurse, di] + workday[nurse, di + 1] - 1
                )

                single_shift_consecutive_penalties.append(penalty)

    # --------------------------------------------------------
    # DEPARTMENT STAFFING REQUIREMENTS
    # --------------------------------------------------------

    coverage_shortages = []

    for department in departments:
        req = department_staffing_requirements[department]

        for di, day in enumerate(DAYS):
            for si, shift in enumerate(SHIFTS):
                required = int(req[shift])

                assigned = sum(
                    x[nurse, department, di, si]
                    for nurse in nurses
                )

                shortage = model.NewIntVar(
                    0, required,
                    f"shortage_{department}_{di}_{si}"
                )

                model.Add(assigned + shortage >= required)
                coverage_shortages.append(shortage)


   # --------------------------------------------------------
   # DEBUG: CHECK EACH NURSE'S AVAILABLE SHIFTS
   # --------------------------------------------------------

    for nurse in nurses:

       print("\nNURSE:", nurse)

    for shift in SHIFTS:

        available_count = sum(
            1
    for day in DAYS
            if (
                not bool(availability_lookup[(nurse, day)]["Off day"])
                and shift in availability_lookup[(nurse, day)]["Available Shifts"]
            )
        )

        print(shift, "available days:", available_count)
    # --------------------------------------------------------
    # NIGHT SHIFT RULES AND INDIVIDUAL SHIFT MINIMUMS
    # --------------------------------------------------------

    consecutive_night_pairs = []
    off_after_night_pairs = []
    morning_evening_after_rest = []
    individual_shift_shortages = []

    for nurse in nurses:

        available_night_days = [
            di
            for di, day in enumerate(DAYS)
            if (
                not bool(availability_lookup[(nurse, day)]["Off day"])
                and "Night" in availability_lookup[(nurse, day)]["Available Shifts"]
            )
        ]

        off_days = [
            di
            for di, day in enumerate(DAYS)
            if bool(availability_lookup[(nurse, day)]["Off day"])
        ]

        # Special nurse: 4 available Night days and 3 Off days.
        is_special_nurse = (
            len(available_night_days) == 4
            and len(off_days) == 3
        )

        if is_special_nurse:

            # All four Night shifts are mandatory.
            for di in available_night_days:
                model.Add(
                    sum(
                        x[nurse, dept, di, 2]
                        for dept in departments
                    ) == 1
                )

        else:

            # Regular nurses: maximum 2 Night shifts per week.
            model.Add(
                sum(
                    x[nurse, dept, di, 2]
                    for dept in departments
                    for di in range(len(DAYS))
                ) <= 2
            )

            # Regular nurses: aim for at least 2 of each shift.
            # If availability makes this impossible, record the shortfall
            # instead of making the entire roster infeasible.
            for si, shift in enumerate(SHIFTS):

                nurse_shift_total = sum(
                    x[nurse, dept, di, si]
                    for dept in departments
                    for di in range(len(DAYS))
                )

                shift_shortfall = model.NewIntVar(
                    0, 2,
                    f"shift_shortfall_{nurses.index(nurse)}_{si}"
                )

                model.Add(nurse_shift_total + shift_shortfall >= 2)

                individual_shift_shortages.append(shift_shortfall)

            # Prefer the two Night shifts to be consecutive.
            for di in range(len(DAYS) - 1):

                night_today = sum(
                    x[nurse, dept, di, 2]
                    for dept in departments
                )

                night_tomorrow = sum(
                    x[nurse, dept, di + 1, 2]
                    for dept in departments
                )

                night_pair = model.NewBoolVar(
                    f"night_pair_{nurses.index(nurse)}_{di}"
                )

                model.Add(night_pair <= night_today)
                model.Add(night_pair <= night_tomorrow)
                model.Add(
                    night_pair >= night_today + night_tomorrow - 1
                )

                consecutive_night_pairs.append(night_pair)

                # Prefer one Off day after the second Night.
                if di + 2 < len(DAYS):

                    off_after_pair = model.NewBoolVar(
                        f"off_after_nights_{nurses.index(nurse)}_{di}"
                    )

                    model.Add(off_after_pair <= night_pair)
                    model.Add(
                        off_after_pair + workday[nurse, di + 2] <= 1
                    )
                    model.Add(
                        off_after_pair >= night_pair - workday[nurse, di + 2]
                    )

                    off_after_night_pairs.append(off_after_pair)

                    # Prefer Morning or Evening on the next two days.
                    for target_day in [di + 3, di + 4]:

                        if target_day < len(DAYS):

                            me_assignments = [
                                x[nurse, dept, target_day, shift_index]
                                for dept in departments
                                for shift_index in [0, 1]
                            ]

                            me_work = model.NewBoolVar(
                                f"morning_evening_{nurses.index(nurse)}_{di}_{target_day}"
                            )

                            model.Add(me_work <= sum(me_assignments))

                            for assignment in me_assignments:
                                model.Add(me_work >= assignment)

                            me_after_rest = model.NewBoolVar(
                                f"me_after_rest_{nurses.index(nurse)}_{di}_{target_day}"
                            )

                            model.Add(me_after_rest <= off_after_pair)
                            model.Add(me_after_rest <= me_work)
                            model.Add(
                                me_after_rest >= off_after_pair + me_work - 1
                            )

                            morning_evening_after_rest.append(me_after_rest)


    # --------------------------------------------------------
    # WEEKLY WORKLOAD FAIRNESS
    # --------------------------------------------------------

    weekly_shift_counts = []

    for nurse in nurses:
        nurse_total = model.NewIntVar(
            0, 14, f"weekly_total_{nurses.index(nurse)}"
        )

        model.Add(
            nurse_total == sum(
                x[nurse, dept, di, si]
                for dept in departments
                for di in range(len(DAYS))
                for si in range(len(SHIFTS))
            )
        )

        weekly_shift_counts.append(nurse_total)

    max_weekly_shifts = model.NewIntVar(0, 14, "max_weekly_shifts")
    min_weekly_shifts = model.NewIntVar(0, 14, "min_weekly_shifts")

    model.AddMaxEquality(max_weekly_shifts, weekly_shift_counts)
    model.AddMinEquality(min_weekly_shifts, weekly_shift_counts)

    workload_spread = model.NewIntVar(0, 14, "workload_spread")
    model.Add(workload_spread == max_weekly_shifts - min_weekly_shifts)

    # --------------------------------------------------------
    # OBJECTIVE: COVERAGE, ALLOCATION, NIGHT PATTERN, FAIRNESS
    # --------------------------------------------------------

    # Reward total shifts allocated, not just the number of days worked.
    total_allocated_shifts = sum(
        x[nurse, dept, di, si]
        for nurse in nurses
        for dept in departments
        for di in range(len(DAYS))
        for si in range(len(SHIFTS))
    )

        model.Minimize(
        2_000_000_000 * sum(coverage_shortages)
        + 100_000_000 * sum(individual_shift_shortages)
        - 1_000_000 * total_allocated_shifts
        - 250_000 * sum(consecutive_night_pairs)
        - 100_000 * sum(off_after_night_pairs)
        - 50_000 * sum(morning_evening_after_rest)
        + 1_000 * workload_spread
        + sum(single_shift_consecutive_penalties)
    )

    # --------------------------------------------------------
    # SOLVE
    # --------------------------------------------------------

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30

    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status

    # --------------------------------------------------------
    # BUILD DEPARTMENT-SPECIFIC ROSTER
    # --------------------------------------------------------

    rows = []

    for nurse in nurses:
        for department in nurse_departments.get(nurse, []):

            row = {
                "Nurse": nurse,
                "Department": department
            }

            for di, day in enumerate(DAYS):

                assigned = [
                    shift
                    for si, shift in enumerate(SHIFTS)
                    if solver.Value(x[nurse, department, di, si])
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
        dept_nurses = [
    n for n in nurses
    if department in nurse_departments.get(n, [])
]
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
    unassigned = [
    n for n in nurses
    if not nurse_departments.get(n)
]
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
        Automated Nurse Roster and Workforce Optimization
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
    ward_beds = st.number_input("Bed capacity", 1, 500, 24, key="ward_beds_gk")
    st.caption("Nurses required per shift")

    ward_morning = st.number_input("Morning", 0, 100, 3, key="ward_morning_gk")
    ward_evening = st.number_input("Evening", 0, 100, 3, key="ward_evening_gk")
    ward_night = st.number_input("Night", 0, 100, 3, key="ward_night_gk")

# Store department staffing requirements for the optimizer
department_staffing_requirements = {
    "ICU": {
        "Morning": icu_morning,
        "Evening": icu_evening,
        "Night": icu_night
    },
    "Ward": {
        "Morning": ward_morning,
        "Evening": ward_evening,
        "Night": ward_night
    }
}


# ============================================================
# 2. NURSE AVAILABILITY
# ============================================================

st.header("2. Nurse Availability")

number_of_nurses = st.number_input(
    "Number of nurses",
    min_value=1,
    max_value=100,
    value=18,
    step=1,
    key="number_of_nurses_gk"
)

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
            f"Name of Nurse {i + 1}",
            placeholder="Enter nurse name",
            key=f"nurse_name_gk_{i}",
            label_visibility="collapsed"
        ).strip()

    icu_key = f"nurse_icu_gk_{i}"
    ward_key = f"nurse_ward_gk_{i}"

    with icu_col:
        st.checkbox("ICU", key=icu_key)

    with ward_col:
        st.checkbox("Ward", key=ward_key)

    if not nurse_name:
        missing_names.append(i + 1)
    else:
        nurses.append(nurse_name)

        selected_departments = []

        if st.session_state[icu_key]:
            selected_departments.append("ICU")

        if st.session_state[ward_key]:
            selected_departments.append("Ward")

        if selected_departments:
            nurse_departments[nurse_name] = selected_departments
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
                (morning_key, True),
                (evening_key, True),
                (night_key, True),
                (off_key, False),
                (single_key, True),
                (double_key, False)
            ]:
                if key not in st.session_state:
                    st.session_state[key] = default

            def handle_off_day(
                mk=morning_key,
                ek=evening_key,
                nk=night_key,
                ok=off_key,
                sk=single_key,
                dk=double_key
            ):
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

            def handle_shift(
                mk=morning_key,
                ek=evening_key,
                nk=night_key,
                ok=off_key
            ):
                if (
                    st.session_state[mk]
                    or st.session_state[ek]
                    or st.session_state[nk]
                ):
                    st.session_state[ok] = False

            def handle_single(sk=single_key, dk=double_key):
                if st.session_state[sk]:
                    st.session_state[dk] = False

            def handle_double(sk=single_key, dk=double_key):
                if st.session_state[dk]:
                    st.session_state[sk] = False

            is_off = st.session_state[off_key]

            st.checkbox(
                "Morning",
                key=morning_key,
                disabled=is_off,
                on_change=handle_shift
            )

            st.checkbox(
                "Evening",
                key=evening_key,
                disabled=is_off,
                on_change=handle_shift
            )

            st.checkbox(
                "Night",
                key=night_key,
                disabled=is_off,
                on_change=handle_shift
            )

            st.checkbox(
                "Off Day",
                key=off_key,
                on_change=handle_off_day
            )

            st.checkbox(
                "Single Shift",
                key=single_key,
                disabled=is_off,
                on_change=handle_single
            )

            st.checkbox(
                "Double Shift",
                key=double_key,
                disabled=is_off,
                on_change=handle_double
            )

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
                    "Single Shift"
                    if st.session_state[single_key]
                    else "Double Shift"
                    if st.session_state[double_key]
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
    
    ward_roster = display_roster[
        display_roster["Department"] == "Ward"
    ][display_columns]

    if ward_roster.empty:
        st.info("No nurses are currently assigned to Ward.")
    else:
        st.dataframe(
            ward_roster,
            use_container_width=True,
            hide_index=True,
            height=500,
            column_config={
                col: st.column_config.Column(width="small")
                for col in ward_roster.columns
            }
        )

    st.subheader("Nurse Workload Summary")

    nurse_shift_counts = {
        row["Nurse"]: sum(assigned_count(row[day]) for day in DAYS)
        for _, row in roster_df.iterrows()
    }

    workload_df = pd.DataFrame([
        {
            "Nurse": nurse,
            "Department": roster_df.loc[
                roster_df["Nurse"] == nurse, "Department"
            ].iloc[0],
            "Assigned Shifts": nurse_shift_counts.get(nurse, 0),
        }
        for nurse in roster_df["Nurse"].tolist()
    ]).sort_values(
        by=["Department", "Assigned Shifts", "Nurse"],
        ascending=[True, False, True]
    )

    st.dataframe(
        workload_df,
        use_container_width=True,
        hide_index=True
    )

    st.subheader("Department Staffing Coverage")

    coverage_rows = []

    for dept in ["ICU", "Ward"]:
        dept_roster = roster_df[roster_df["Department"] == dept]

        for shift in SHIFTS:
            for day in DAYS:
                actual = sum(
                    1 for cell in dept_roster[day]
                    if shift in (
                        cell.split(" + ")
                        if isinstance(cell, str) else []
                    )
                )

                required = int(
                    department_staffing_requirements[dept][shift]
                )

                coverage_rows.append({
                    "Department": dept,
                    "Day": day,
                    "Shift": shift,
                    "Required": required,
                    "Assigned": actual,
                    "Status": "Met" if actual >= required else "Short"
                })

    

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
