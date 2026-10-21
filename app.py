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

# Light-blue healthcare-style interface
st.markdown(
    """
    <style>
    /* Main headings */
    h1, h2, h3 {
        color: #234E70;
    }

    /* Checkbox accent */
    [data-testid="stCheckbox"] div[data-baseweb="checkbox"] > div:first-child {
    background-color: #7BB7D9 !important;
    border-color: #7BB7D9 !important;
}

[data-testid="stCheckbox"] div[data-baseweb="checkbox"] svg {
    fill: white !important;
}

    /* Availability labels */
    .availability-note {
        background-color: #F3F9FC;
        border-left: 4px solid #7BB7D9;
        padding: 12px 16px;
        border-radius: 6px;
        margin-bottom: 15px;
    }

    /* Capacity cards */
    .capacity-card {
        background-color: #F3F9FC;
        border: 1px solid #D7EAF3;
        border-radius: 10px;
        padding: 18px;
        text-align: center;
    }

    .capacity-number {
        font-size: 28px;
        font-weight: 700;
        color: #234E70;
    }

    .capacity-label {
        font-size: 14px;
        color: #526777;
    }
    </style>
    """,
    unsafe_allow_html=True
)


# ============================================================
# CONSTANTS
# ============================================================

DAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday"
]

SHIFTS = [
    "Morning",
    "Evening",
    "Night"
]

# Assumption used for the early capacity estimate
ASSUMED_MAX_SHIFTS_PER_NURSE = 6


# ============================================================
# SESSION STATE
# ============================================================

if "roster" not in st.session_state:
    st.session_state["roster"] = None


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def calculate_weekly_requirement(staffing_requirements_df):
    """Total number of nurse-shifts required in the week."""
    return int(
        staffing_requirements_df[SHIFTS].sum().sum()
    )


def estimate_minimum_nurses(
    staffing_requirements_df,
    max_shifts_per_nurse=ASSUMED_MAX_SHIFTS_PER_NURSE
):
    """
    Early theoretical capacity estimate.

    This does NOT yet consider individual availability,
    rest constraints, preferences, or skill mix.
    """
    weekly_requirement = calculate_weekly_requirement(
        staffing_requirements_df
    )

    if max_shifts_per_nurse <= 0:
        return 0

    return math.ceil(
        weekly_requirement / max_shifts_per_nurse
    )


def normalize_availability(selected_shifts):
    """Convert availability selections into usable shift list."""
    return [
        shift for shift in SHIFTS
        if shift in selected_shifts
    ]


def generate_roster(
    nurses,
    availability_df,
    preferences_df,
    staffing_requirements_df,
    max_shifts_per_nurse
):
    """Generate optimized weekly nurse roster using CP-SAT."""

    model = cp_model.CpModel()

    # --------------------------------------------------------
    # Decision variables
    # --------------------------------------------------------

    x = {}

    for nurse in nurses:
        for day_index, day in enumerate(DAYS):
            for shift_index, shift in enumerate(SHIFTS):

                x[nurse, day_index, shift_index] = model.NewBoolVar(
                    f"x_{nurse}_{day_index}_{shift_index}"
                )

    # --------------------------------------------------------
    # Constraint 1: Maximum one shift per nurse per day
    # --------------------------------------------------------

    for nurse in nurses:
        for day_index in range(len(DAYS)):

            model.Add(
                sum(
                    x[nurse, day_index, shift_index]
                    for shift_index in range(len(SHIFTS))
                ) <= 1
            )

    # --------------------------------------------------------
    # Constraint 2: Availability / Off day
    # --------------------------------------------------------

    for _, row in availability_df.iterrows():

        nurse = row["Nurse"]
        day_index = DAYS.index(row["Day"])

        if row["Off day"]:
            for shift_index in range(len(SHIFTS)):
                model.Add(
                    x[nurse, day_index, shift_index] == 0
                )

        else:
            selected = row["Available Shifts"]

            for shift_index, shift in enumerate(SHIFTS):

                if shift not in selected:
                    model.Add(
                        x[nurse, day_index, shift_index] == 0
                    )

    # --------------------------------------------------------
    # Constraint 3: Staffing requirements
    # --------------------------------------------------------

    for day_index, day in enumerate(DAYS):

        staffing_row = staffing_requirements_df[
            staffing_requirements_df["Day"] == day
        ].iloc[0]

        for shift_index, shift in enumerate(SHIFTS):

            required = int(staffing_row[shift])

            model.Add(
                sum(
                    x[nurse, day_index, shift_index]
                    for nurse in nurses
                ) >= required
            )

    # --------------------------------------------------------
    # Constraint 4: Maximum shifts per nurse per week
    # --------------------------------------------------------

    for nurse in nurses:

        model.Add(
            sum(
                x[nurse, day_index, shift_index]
                for day_index in range(len(DAYS))
                for shift_index in range(len(SHIFTS))
            ) <= max_shifts_per_nurse
        )

    # --------------------------------------------------------
    # Constraint 5: Minimum rest = 2 shifts
    #
    # A nurse needs TWO complete shift periods between
    # consecutive assignments.
    #
    # Therefore assignments whose shift-slot distance is
    # 1 or 2 cannot both occur.
    #
    # Example:
    # Monday Morning -> Tuesday Morning
    # has Monday Evening + Monday Night between them,
    # so it IS allowed.
    # --------------------------------------------------------

    all_slots = []

    for day_index in range(len(DAYS)):
        for shift_index in range(len(SHIFTS)):
            slot_index = day_index * len(SHIFTS) + shift_index
            all_slots.append(
                (day_index, shift_index, slot_index)
            )

    for nurse in nurses:

        for i in range(len(all_slots)):

            day_1, shift_1, slot_1 = all_slots[i]

            for j in range(i + 1, len(all_slots)):

                day_2, shift_2, slot_2 = all_slots[j]

                slot_gap = slot_2 - slot_1

                if slot_gap <= 2:

                    model.Add(
                        x[nurse, day_1, shift_1]
                        +
                        x[nurse, day_2, shift_2]
                        <= 1
                    )

    # --------------------------------------------------------
    # Objective
    #
    # 1. Reward preferred shifts
    # 2. Penalize unnecessary assignments
    # --------------------------------------------------------

    objective_terms = []

    for nurse in nurses:

        preferred_shift = preferences_df.loc[
            preferences_df["Nurse"] == nurse,
            "Preferred Shift"
        ].iloc[0]

        for day_index in range(len(DAYS)):
            for shift_index, shift in enumerate(SHIFTS):

                variable = x[nurse, day_index, shift_index]

                # Strong reward for preferred shift
                if preferred_shift == shift:
                    objective_terms.append(
                        100 * variable
                    )

                # Small penalty for every assignment
                objective_terms.append(
                    -1 * variable
                )

    model.Maximize(
        sum(objective_terms)
    )

    # --------------------------------------------------------
    # Solve
    # --------------------------------------------------------

    solver = cp_model.CpSolver()

    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 8

    status = solver.Solve(model)

    if status not in [
        cp_model.OPTIMAL,
        cp_model.FEASIBLE
    ]:
        return None, status

    # --------------------------------------------------------
    # Create roster output
    # --------------------------------------------------------

    roster_data = []

    for nurse in nurses:

        row = {"Nurse": nurse}

        for day_index, day in enumerate(DAYS):

            assigned_shift = "Off"

            for shift_index, shift in enumerate(SHIFTS):

                if solver.Value(
                    x[nurse, day_index, shift_index]
                ):
                    assigned_shift = shift
                    break

            row[day] = assigned_shift

        roster_data.append(row)

    roster_df = pd.DataFrame(roster_data)

    return roster_df, status


def calculate_roster_metrics(
    roster_df,
    staffing_requirements_df
):
    """Calculate basic roster coverage metrics."""

    total_required = calculate_weekly_requirement(
        staffing_requirements_df
    )

    total_assigned = 0

    for day in DAYS:

        for shift in SHIFTS:

            total_assigned += (
                roster_df[day] == shift
            ).sum()

    coverage = (
        (total_assigned / total_required) * 100
        if total_required > 0
        else 100
    )

    return total_required, total_assigned, coverage


# ============================================================
# TITLE
# ============================================================

st.title("🏥 Nurse Roster Optimizer")

st.caption(
    "Generate a weekly nurse roster based on staffing requirements, "
    "availability, preferences and scheduling constraints."
)


# ============================================================
# 1. STAFFING REQUIREMENTS
# ============================================================

st.header("1. Staffing Requirements")

st.write(
    "Enter the minimum number of nurses required for each shift."
)

default_staffing = pd.DataFrame({
    "Day": DAYS,
    "Morning": [None] * 7,
    "Evening": [None] * 7,
    "Night": [None] * 7
})

staffing_requirements_df = st.data_editor(
    default_staffing,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Day": st.column_config.TextColumn(
            "Day",
            disabled=True
        ),
        "Morning": st.column_config.NumberColumn(
            "Morning",
            min_value=0,
            step=1
        ),
        "Evening": st.column_config.NumberColumn(
            "Evening",
            min_value=0,
            step=1
        ),
        "Night": st.column_config.NumberColumn(
            "Night",
            min_value=0,
            step=1
        )
    },
    key="staffing_requirements_v4"
)

# ------------------------------------------------------------
# Immediate workforce estimate
# ------------------------------------------------------------

staffing_complete = staffing_requirements_df[SHIFTS].notna().all().all()

if staffing_complete:
    weekly_requirement = calculate_weekly_requirement(
        staffing_requirements_df
    )

    estimated_nurses = estimate_minimum_nurses(
        staffing_requirements_df
    )

    st.subheader("Workforce Requirement")

    st.markdown(
        f"""
        <div class="availability-note">
        <b>Estimated minimum nurses required: {estimated_nurses}</b><br>
        Based on <b>{weekly_requirement} nurse-shifts</b> required during the week,
        assuming a maximum of <b>{ASSUMED_MAX_SHIFTS_PER_NURSE} shifts per nurse per week</b>.
        </div>
        """,
        unsafe_allow_html=True
    )

    st.caption(
        "This is an early capacity estimate. Actual roster feasibility "
        "will also depend on nurse availability, off days, preferences "
        "and minimum-rest requirements."
    )
else:
    weekly_requirement = 0
    st.info(
        "Enter the minimum staffing requirement for every day and shift "
        "to calculate the weekly workforce requirement."
    )


# ============================================================
# 2. NURSE INFORMATION
# ============================================================

st.header("2. Nurse Information")

number_of_nurses_input = st.number_input(
    "Number of nurses",
    min_value=1,
    max_value=100,
    value=None,
    step=1,
    placeholder="Enter number of nurses",
    key="number_of_nurses_v4"
)

number_of_nurses = (
    int(number_of_nurses_input)
    if number_of_nurses_input is not None
    else 0
)

nurses = []

columns = st.columns(3)

for i in range(number_of_nurses):

    with columns[i % 3]:

        nurse_name = st.text_input(
            f"Nurse {i + 1}",
            placeholder="Enter nurse name",
            key=f"nurse_name_v4_{i}"
        ).strip()

        if nurse_name:
            nurses.append(nurse_name)

if len(nurses) < number_of_nurses:

    st.info(
        f"Please enter all {number_of_nurses} nurse names "
        "before generating the roster."
    )

elif len(set(nurses)) != len(nurses):

    st.error(
        "Nurse names must be unique."
    )


# ============================================================
# 3. NURSE AVAILABILITY & OFF DAYS
# ============================================================

st.header("3. Nurse Availability & Off Days")
st.markdown(
    """
    <div class="availability-note">
    <b>All shifts are selected by default.</b><br>
    Deselect any shift when the nurse is not available.
    Select <b>Off day</b> when that specific day is the nurse's
    planned/requested day off.
    </div>
    """,
    unsafe_allow_html=True
)

availability_records = []

if len(nurses) == number_of_nurses and len(set(nurses)) == len(nurses):

    for nurse_index, nurse in enumerate(nurses):

        st.subheader(nurse)

        day_columns = st.columns(7)

        for day_index, day in enumerate(DAYS):

            with day_columns[day_index]:

                st.markdown(f"**{day[:3]}**")

                morning_key = f"morning_v4_{nurse_index}_{day_index}"
                evening_key = f"evening_v4_{nurse_index}_{day_index}"
                night_key = f"night_v4_{nurse_index}_{day_index}"
                off_key = f"off_v4_{nurse_index}_{day_index}"

                # Initialize defaults once. All shifts start selected.
                if morning_key not in st.session_state:
                    st.session_state[morning_key] = True
                if evening_key not in st.session_state:
                    st.session_state[evening_key] = True
                if night_key not in st.session_state:
                    st.session_state[night_key] = True
                if off_key not in st.session_state:
                    st.session_state[off_key] = False

                def handle_off_day(
                    morning_key=morning_key,
                    evening_key=evening_key,
                    night_key=night_key,
                    off_key=off_key
                ):
                    if st.session_state[off_key]:
                        st.session_state[morning_key] = False
                        st.session_state[evening_key] = False
                        st.session_state[night_key] = False
                    else:
                        st.session_state[morning_key] = True
                        st.session_state[evening_key] = True
                        st.session_state[night_key] = True

                def handle_shift(
                    morning_key=morning_key,
                    evening_key=evening_key,
                    night_key=night_key,
                    off_key=off_key
                ):
                    if (
                        st.session_state.get(morning_key, False)
                        or st.session_state.get(evening_key, False)
                        or st.session_state.get(night_key, False)
                    ):
                        st.session_state[off_key] = False

                off_day_current = st.session_state.get(off_key, False)

                morning = st.checkbox(
                    "Morning", key=morning_key, disabled=off_day_current,
                    on_change=handle_shift
                )
                evening = st.checkbox(
                    "Evening", key=evening_key, disabled=off_day_current,
                    on_change=handle_shift
                )
                night = st.checkbox(
                    "Night", key=night_key, disabled=off_day_current,
                    on_change=handle_shift
                )
                off_day = st.checkbox(
                    "Off day", key=off_key, on_change=handle_off_day
                )

                selected_shifts = []

                if morning:
                    selected_shifts.append("Morning")

                if evening:
                    selected_shifts.append("Evening")

                if night:
                    selected_shifts.append("Night")

                if off_day:
                    selected_shifts = []

                availability_records.append({
                    "Nurse": nurse,
                    "Day": day,
                    "Available Shifts": selected_shifts,
                    "Off day": off_day
                })

    availability_df = pd.DataFrame(availability_records)

else:
    availability_df = pd.DataFrame()

# 4 Preferences

# ============================================================
# 4. NURSE PREFERENCES
# ============================================================

st.header("4. Nurse Preferences")

st.write(
    "Preferences are used by the optimizer where possible. "
    "They do not override staffing or safety constraints."
)

preferences_records = []

if len(nurses) == number_of_nurses and len(set(nurses)) == len(nurses):

    preference_columns = st.columns(3)

    for nurse_index, nurse in enumerate(nurses):

        with preference_columns[nurse_index % 3]:

            preferred_shift = st.selectbox(
                f"{nurse} — Preferred shift",
                ["Select preference", "No preference"] + SHIFTS,
                index=0,
                key=f"preferred_shift_v4_{nurse_index}"
            )

            preferences_records.append({
                "Nurse": nurse,
                "Preferred Shift": preferred_shift
            })

    preferences_df = pd.DataFrame(
        preferences_records
    )

else:

    preferences_df = pd.DataFrame(
        columns=[
            "Nurse",
            "Preferred Shift"
        ]
    )


# ============================================================
# 5. SCHEDULING RULES
# ============================================================

st.header("5. Scheduling Rules & Constraints")

st.markdown(
    """
    The optimizer applies the following rules when generating the roster.
    Hard constraints must be satisfied; preferences are considered where
    possible without violating those constraints.
    """
)

st.subheader("🔒 Hard Constraints — Must Be Satisfied")

hard_constraints = pd.DataFrame({
    "Constraint": [
        "Minimum staffing coverage",
        "One shift per nurse per day",
        "Nurse availability",
        "Off day",
        "Maximum shifts per nurse per week",
        "Minimum rest between shifts"
    ],
    "What it means": [
        "The required number of nurses must be assigned to every day and shift.",
        "A nurse cannot be assigned to more than one shift on the same day.",
        "A nurse can only be assigned to shifts they have marked as available.",
        "A nurse marked as Off cannot be assigned any shift on that day.",
        "Each nurse cannot exceed the weekly maximum entered below.",
        "At least 2 complete shift periods must separate consecutive assignments."
    ]
})

st.dataframe(
    hard_constraints,
    use_container_width=True,
    hide_index=True
)

st.subheader("⭐ Preference — Optimized Where Possible")

st.markdown(
    """
    **Preferred shift:** The optimizer tries to assign each nurse to their
    preferred shift when possible. A preference does not override staffing,
    availability, rest, or workload constraints.
    """
)

rule_columns = st.columns(2)

with rule_columns[0]:
    max_shifts_per_nurse = st.number_input(
        "Maximum shifts per nurse per week",
        min_value=1,
        max_value=7,
        value=None,
        step=1,
        placeholder="Enter maximum shifts",
        key="max_shifts_v4"
    )

max_shifts_per_nurse = (
    int(max_shifts_per_nurse)
    if max_shifts_per_nurse is not None
    else 0
)

with rule_columns[1]:
    minimum_rest_shifts = st.number_input(
        "Minimum rest between shifts",
        min_value=0,
        max_value=5,
        value=2,
        step=1,
        disabled=True,
        key="minimum_rest_v4"
    )

st.caption(
    "Currently uses a minimum rest requirement of 2 complete shift "
    "periods between consecutive assignments."
)

# ============================================================
# 6. FINAL FEASIBILITY CHECK
# ============================================================

st.header("6. Feasibility Check")

if (
    staffing_complete
    and max_shifts_per_nurse > 0
    and len(nurses) == number_of_nurses
    and len(set(nurses)) == len(nurses)
):

    weekly_capacity = (
        len(nurses) * max_shifts_per_nurse
    )

    capacity_difference = (
        weekly_capacity - weekly_requirement
    )

    metric_columns = st.columns(3)

    with metric_columns[0]:
        st.metric(
            "Weekly shifts required",
            weekly_requirement
        )

    with metric_columns[1]:
        st.metric(
            "Available nurse capacity",
            weekly_capacity
        )

    with metric_columns[2]:
        st.metric(
            "Nurses entered",
            len(nurses)
        )

    if capacity_difference >= 0:

        st.success(
            f"Theoretical capacity check passed. The entered workforce has "
            f"{weekly_capacity} maximum weekly nurse-shifts against "
            f"{weekly_requirement} required nurse-shifts. This confirms "
            "basic numerical capacity, but does not guarantee that a "
            "feasible roster can be generated after applying availability, "
            "rest and scheduling constraints."
        )

    else:

        shortage = abs(capacity_difference)

        st.warning(
            f"Potential capacity shortage: approximately "
            f"{shortage} additional nurse-shifts are needed. "
            f"Consider adding nurses, increasing allowable shifts, "
            f"or reducing staffing requirements."
        )

    # --------------------------------------------------------
    # Day/shift availability check
    # --------------------------------------------------------

    availability_shortages = []

    for day in DAYS:

        for shift in SHIFTS:

            available_count = 0

            if not availability_df.empty:

                for _, row in availability_df[
                    availability_df["Day"] == day
                ].iterrows():

                    if (
                        not row["Off day"]
                        and shift in row["Available Shifts"]
                    ):
                        available_count += 1

            required = int(
                staffing_requirements_df.loc[
                    staffing_requirements_df["Day"] == day,
                    shift
                ].iloc[0]
            )

            if available_count < required:

                availability_shortages.append({
                    "Day": day,
                    "Shift": shift,
                    "Required": required,
                    "Available": available_count,
                    "Shortage": required - available_count
                })

    if availability_shortages:

        st.warning(
            "Some day/shift combinations do not currently have "
            "enough available nurses."
        )

        shortage_df = pd.DataFrame(
            availability_shortages
        )

        st.dataframe(
            shortage_df,
            use_container_width=True,
            hide_index=True
        )

    else:

        st.success(
            "Basic availability check passed. Each individual day/shift "
            "has enough available nurses. Overall roster feasibility will "
            "be determined by the optimizer after applying all scheduling "
            "constraints."
        )


# ============================================================
# 7. GENERATE ROSTER
# ============================================================

st.header("7. Generate Optimized Roster")

generate_button = st.button(
    "Generate Weekly Roster",
    type="primary",
    use_container_width=True
)

if generate_button:

    if not staffing_complete:

        st.error(
            "Please enter the minimum staffing requirement for every day and shift."
        )

    elif number_of_nurses == 0:

        st.error(
            "Please enter the number of nurses."
        )

    elif max_shifts_per_nurse == 0:

        st.error(
            "Please enter the maximum shifts allowed per nurse per week."
        )

    elif len(nurses) != number_of_nurses:

        st.error(
            "Please enter all nurse names."
        )

    elif len(set(nurses)) != len(nurses):

        st.error(
            "Please make sure all nurse names are unique."
        )

    elif availability_df.empty:

        st.error(
            "Please complete nurse availability."
        )

    else:

        with st.spinner(
            "Optimizing the weekly roster..."
        ):

            roster_df, status = generate_roster(
                nurses,
                availability_df,
                preferences_df,
                staffing_requirements_df,
                max_shifts_per_nurse
            )

        if roster_df is None:

            st.error(
                "No feasible roster could be generated. Although basic "
                "capacity and individual shift availability are sufficient, "
                "the complete set of scheduling constraints cannot be "
                "satisfied simultaneously."
            )

            st.info(
                "Review nurse availability, Off days, maximum shifts per "
                "nurse and the minimum-rest requirement. The theoretical "
                "capacity and preliminary availability checks do not test "
                "all constraints together."
            )

        else:

            st.session_state["roster"] = roster_df

            st.success(
                "A feasible weekly roster has been generated."
            )


# ============================================================
# 8. ROSTER OUTPUT
# ============================================================

if st.session_state["roster"] is not None:

    st.header("8. Weekly Roster")

    roster_df = st.session_state["roster"]

    # --------------------------------------------------------
    # Calculate roster metrics
    # --------------------------------------------------------

    total_required, total_assigned, coverage = (
        calculate_roster_metrics(
            roster_df,
            staffing_requirements_df
        )
    )

    total_capacity = len(nurses) * max_shifts_per_nurse
    unused_capacity = total_capacity - total_assigned

    excess_assignments = max(
        0,
        total_assigned - total_required
    )

    # --------------------------------------------------------
    # Calculate nurse workload
    # --------------------------------------------------------

    nurse_shift_counts = {}

    for _, row in roster_df.iterrows():
        nurse_shift_counts[row["Nurse"]] = sum(
            row[day] in SHIFTS
            for day in DAYS
        )

    # ========================================================
    # ROSTER SUMMARY
    # ========================================================

    st.subheader("Roster Summary")

    card_columns = st.columns(4)

    # Required nurse-shifts
    with card_columns[0]:
        st.markdown(
            f"""
            <div style="
                background-color:#EAF4FB;
                border:1px solid #B8D8EA;
                border-radius:12px;
                padding:18px;
                text-align:center;
            ">
                <div style="font-size:28px;">👥</div>
                <div style="font-size:14px;color:#555;">
                    Required nurse-shifts
                </div>
                <div style="
                    font-size:28px;
                    font-weight:bold;
                    color:#234E70;
                ">
                    {total_required}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    # Assigned nurse-shifts
    with card_columns[1]:
        st.markdown(
            f"""
            <div style="
                background-color:#EAF7F0;
                border:1px solid #B9DFC9;
                border-radius:12px;
                padding:18px;
                text-align:center;
            ">
                <div style="font-size:28px;">✅</div>
                <div style="font-size:14px;color:#555;">
                    Assigned nurse-shifts
                </div>
                <div style="
                    font-size:28px;
                    font-weight:bold;
                    color:#2E6B4A;
                ">
                    {total_assigned}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    # Weekly capacity
    with card_columns[2]:
        st.markdown(
            f"""
            <div style="
                background-color:#FFF6E5;
                border:1px solid #E8D2A3;
                border-radius:12px;
                padding:18px;
                text-align:center;
            ">
                <div style="font-size:28px;">📊</div>
                <div style="font-size:14px;color:#555;">
                    Weekly capacity
                </div>
                <div style="
                    font-size:28px;
                    font-weight:bold;
                    color:#80652A;
                ">
                    {total_capacity}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    # Unused capacity
    with card_columns[3]:
        st.markdown(
            f"""
            <div style="
                background-color:#F3ECF8;
                border:1px solid #D5C2E2;
                border-radius:12px;
                padding:18px;
                text-align:center;
            ">
                <div style="font-size:28px;">📉</div>
                <div style="font-size:14px;color:#555;">
                    Unused capacity
                </div>
                <div style="
                    font-size:28px;
                    font-weight:bold;
                    color:#684A7A;
                ">
                    {unused_capacity}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

    st.write("")

    # ========================================================
    # WEEKLY ROSTER
    # ========================================================

    st.markdown(
        """
        <h2 style="
            text-align:center;
            color:#234E70;
            margin-top:20px;
        ">
            📅 Weekly Roster
        </h2>
        """,
        unsafe_allow_html=True
    )

    st.dataframe(
        roster_df,
        use_container_width=True,
        hide_index=True
    )

    # ========================================================
    # NURSE WORKLOAD
    # ========================================================

    st.markdown(
        """
        <h2 style="
            text-align:center;
            color:#234E70;
            margin-top:30px;
        ">
            Nurse Workload Summary
        </h2>
        """,
        unsafe_allow_html=True
    )

    workload_df = pd.DataFrame(
        [
            {
                "Nurse": nurse,
                "Assigned Shifts": nurse_shift_counts.get(
                    nurse,
                    0
                ),
                "Maximum Allowed": max_shifts_per_nurse,
                "Remaining Capacity": max(
                    0,
                    max_shifts_per_nurse
                    - nurse_shift_counts.get(nurse, 0)
                )
            }
            for nurse in nurses
        ]
    )

    workload_df = workload_df.sort_values(
        by=["Assigned Shifts", "Nurse"],
        ascending=[False, True]
    )

    st.dataframe(
        workload_df,
        use_container_width=True,
        hide_index=True
    )

    # ========================================================
    # KEY OBSERVATIONS
    # ========================================================

    st.markdown(
        """
        <h2 style="
            text-align:center;
            color:#234E70;
            margin-top:30px;
        ">
            Key Observations
        </h2>
        """,
        unsafe_allow_html=True
    )

    # Workforce capacity observation
    if unused_capacity > 0:
        st.markdown(
            f"""
            <div style="
                background-color:#FFF8E8;
                border-left:6px solid #E0B84C;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>📊 Workforce capacity:</b>
                The roster uses {total_assigned} of
                {total_capacity} available nurse-shifts,
                leaving {unused_capacity} unused capacity.
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"""
            <div style="
                background-color:#EAF7F0;
                border-left:6px solid #6BB38A;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>📊 Workforce capacity:</b>
                All available weekly nurse-shift capacity
                is utilized.
            </div>
            """,
            unsafe_allow_html=True
        )

    # Staffing observation
    if excess_assignments > 0:
        st.markdown(
            f"""
            <div style="
                background-color:#EAF4FB;
                border-left:6px solid #7BB7D9;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>📌 Staffing:</b>
                The generated roster contains
                {excess_assignments} nurse-shifts above
                the minimum staffing requirement.
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            """
            <div style="
                background-color:#EAF4FB;
                border-left:6px solid #7BB7D9;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>📌 Staffing:</b>
                The roster meets the minimum staffing
                requirements without additional assignments.
            </div>
            """,
            unsafe_allow_html=True
        )

    # Workload observation
    nurses_below_limit = workload_df[
        workload_df["Assigned Shifts"]
        < workload_df["Maximum Allowed"]
    ]

    if not nurses_below_limit.empty:

        nurse_names = ", ".join(
            nurses_below_limit["Nurse"].tolist()
        )

        st.markdown(
            f"""
            <div style="
                background-color:#F3ECF8;
                border-left:6px solid #A982BC;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>👩‍⚕️ Workload:</b>
                The following nurses are below their weekly
                shift limit: {nurse_names}.
            </div>
            """,
            unsafe_allow_html=True
        )

    else:

        st.markdown(
            """
            <div style="
                background-color:#F3ECF8;
                border-left:6px solid #A982BC;
                border-radius:8px;
                padding:14px;
                margin-bottom:10px;
            ">
                <b>👩‍⚕️ Workload:</b>
                All nurses are assigned up to their configured
                weekly shift limit.
            </div>
            """,
            unsafe_allow_html=True
        )

    # ========================================================
    # DOWNLOAD ROSTER
    # ========================================================

    st.markdown("### Download Roster")

    csv_data = roster_df.to_csv(index=False)

    st.download_button(
        label="Download Roster as CSV",
        data=csv_data,
        file_name="nurse_roster.csv",
        mime="text/csv"
    )

# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "Nurse Roster Optimizer V4 | "
    "Python • OR-Tools CP-SAT • Streamlit"
)
