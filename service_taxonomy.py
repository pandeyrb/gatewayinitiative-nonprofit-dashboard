"""
Canonical service taxonomy for the GWI dashboard.

Two layers, from two sources:

  * **Categories** are the boss's categorization (2026-09-08 draft). They are
    the browse headings and follow standard resource-directory practice —
    clothing is its own category rather than living under food, faith and civic
    services are separated, and mental health is a peer of clinical care rather
    than a child of it.

  * **Canonical tags** are the leaf filter options. The `Services` column is
    free text authored per organization, so raw tags are effectively unique:
    485 distinct strings across 65 orgs, 468 appearing exactly once. The boss's
    draft groups those raw strings directly under categories, which leaves
    "Food Pantry", "Food Pantry Services", "Emergency Food Pantry", "Mobile
    Pantry" and "Soup Kitchen" as five separate options under one heading. The
    canonical layer collapses those into one option each without flattening the
    category structure.

The goal is browsable, NOT generic: "Food Pantry", "Community Meals" and
"SNAP & WIC Enrollment" stay three distinct tags. What gets merged is wording,
not meaning.

A raw tag can map to more than one canonical tag — see ALSO. That idea comes
from the boss's draft and is a real improvement: community gardens belong under
both food and environment, music therapy under both mental health and arts.

Raw wording is never lost — the map popup, directory table and detail panel all
still render the original `Services` text. Only the filter is canonical.

Changes made to the boss's draft are documented in `qa/categorization_review.md`
for their review. Adding organizations later: run
`scripts/audit_service_tags.py` to see how every raw tag resolved and which
fell through to UNMATCHED.
"""

import csv
import os
import re

# ── categories ────────────────────────────────────────────────────────────────
# The boss's 25, plus Domestic & Sexual Violence Services (see the review memo —
# the draft has no home for Jeanne Geiger, YWCA or Northeast Justice Center's
# DV work). Ordered by urgency, so someone in crisis is not scrolling past
# "Community Events" to reach shelter.
CATEGORY_ORDER = [
    "Food Assistance",
    "Clothing & Household Essentials",
    "Homelessness & Emergency Shelter",
    "Homebuyers and Renters Assistance",
    "Utility & Emergency Financial Aid",
    "Financial Counseling",
    "Health Care (Clinical)",
    "Mental & Behavioral Health",
    "Domestic & Sexual Violence Services",
    "Disability & Aging Services",
    "Family Support & Child Welfare",
    "Early Childhood & Child Care",
    "K-12 Academic Support",
    "Youth Development & Mentoring",
    "College & Career Readiness",
    "Adult Education & ESL",
    "Employment & Workforce Training",
    "Legal & Immigration",
    "Housing & Economic Development",
    "Small Business & Nonprofit Support",
    "Sports, Recreation & Fitness",
    "Arts & Creative Programs",
    "Faith & Spiritual Services",
    "Environmental Programs",
    "Civic & Government Services",
    "Community Events & Public Life",
]

CATEGORY_ES = {
    "Food Assistance": "Asistencia Alimentaria",
    "Clothing & Household Essentials": "Ropa y Artículos del Hogar",
    "Homelessness & Emergency Shelter": "Personas sin Hogar y Refugio",
    "Homebuyers and Renters Assistance": "Ayuda para Compradores e Inquilinos",
    "Utility & Emergency Financial Aid": "Servicios Públicos y Ayuda de Emergencia",
    "Financial Counseling": "Asesoría Financiera",
    "Health Care (Clinical)": "Atención Médica",
    "Mental & Behavioral Health": "Salud Mental y del Comportamiento",
    "Domestic & Sexual Violence Services": "Violencia Doméstica y Sexual",
    "Disability & Aging Services": "Discapacidad y Adultos Mayores",
    "Family Support & Child Welfare": "Apoyo Familiar y Bienestar Infantil",
    "Early Childhood & Child Care": "Primera Infancia y Cuidado Infantil",
    "K-12 Academic Support": "Apoyo Académico K-12",
    "Youth Development & Mentoring": "Desarrollo Juvenil y Mentoría",
    "College & Career Readiness": "Preparación Universitaria y Profesional",
    "Adult Education & ESL": "Educación para Adultos e Inglés",
    "Employment & Workforce Training": "Empleo y Capacitación Laboral",
    "Legal & Immigration": "Legal e Inmigración",
    "Housing & Economic Development": "Vivienda y Desarrollo Económico",
    "Small Business & Nonprofit Support": "Pequeños Negocios y Organizaciones",
    "Sports, Recreation & Fitness": "Deportes, Recreación y Ejercicio",
    "Arts & Creative Programs": "Artes y Programas Creativos",
    "Faith & Spiritual Services": "Servicios de Fe y Espirituales",
    "Environmental Programs": "Programas Ambientales",
    "Civic & Government Services": "Servicios Cívicos y Gubernamentales",
    "Community Events & Public Life": "Eventos Comunitarios y Vida Pública",
}

# ── canonical tags ────────────────────────────────────────────────────────────
# canonical tag -> (category, Spanish label)
#
# A fixed tag set is what makes Spanish filter labels possible at all. The raw
# column is English-only, so without this the Spanish UI showed 485 English
# service names.
TAGS: dict[str, tuple[str, str]] = {
    # Food Assistance
    "Food Pantry": ("Food Assistance", "Despensa de Alimentos"),
    "Community Meals": ("Food Assistance", "Comidas Comunitarias"),
    "Food Distribution & Access": ("Food Assistance", "Distribución y Acceso a Alimentos"),
    "SNAP & WIC Enrollment": ("Food Assistance", "Inscripción a SNAP y WIC"),
    # Clothing & Household Essentials
    "Clothing & Essential Goods": (
        "Clothing & Household Essentials",
        "Ropa y Artículos Esenciales",
    ),
    "Diapers & Baby Supplies": (
        "Clothing & Household Essentials",
        "Pañales y Artículos para Bebé",
    ),
    # Homelessness & Emergency Shelter
    "Emergency Shelter": ("Homelessness & Emergency Shelter", "Refugio de Emergencia"),
    "Transitional Housing": (
        "Homelessness & Emergency Shelter",
        "Vivienda de Transición",
    ),
    # Homebuyers and Renters Assistance
    "Rental Assistance & Tenant Rights": (
        "Homebuyers and Renters Assistance",
        "Ayuda de Alquiler y Derechos del Inquilino",
    ),
    "Homebuyer Education": (
        "Homebuyers and Renters Assistance",
        "Educación para Compradores de Vivienda",
    ),
    "Foreclosure Prevention": (
        "Homebuyers and Renters Assistance",
        "Prevención de Ejecución Hipotecaria",
    ),
    # Utility & Emergency Financial Aid
    "Utility & Energy Bill Assistance": (
        "Utility & Emergency Financial Aid",
        "Ayuda con Facturas de Servicios",
    ),
    "Home Repair & Energy Efficiency": (
        "Utility & Emergency Financial Aid",
        "Reparación del Hogar y Eficiencia Energética",
    ),
    "Public Benefits Assistance": (
        "Utility & Emergency Financial Aid",
        "Ayuda con Beneficios Públicos",
    ),
    # Financial Counseling
    "Financial Coaching & Credit Counseling": (
        "Financial Counseling",
        "Asesoría Financiera y de Crédito",
    ),
    "Tax Preparation": ("Financial Counseling", "Preparación de Impuestos"),
    # Health Care (Clinical)
    "Primary & Walk-In Care": ("Health Care (Clinical)", "Atención Primaria y sin Cita"),
    "Pediatric Care": ("Health Care (Clinical)", "Atención Pediátrica"),
    "Women's Health & Prenatal Care": (
        "Health Care (Clinical)",
        "Salud de la Mujer y Prenatal",
    ),
    "Pharmacy Services": ("Health Care (Clinical)", "Servicios de Farmacia"),
    "Nutrition & Wellness": ("Health Care (Clinical)", "Nutrición y Bienestar"),
    "In-Home & Residential Care": (
        "Health Care (Clinical)",
        "Cuidado en el Hogar y Residencial",
    ),
    "Health Insurance Enrollment": (
        "Health Care (Clinical)",
        "Inscripción al Seguro Médico",
    ),
    # Mental & Behavioral Health
    "Mental Health Counseling": (
        "Mental & Behavioral Health",
        "Consejería de Salud Mental",
    ),
    "Psychiatry & Medication": ("Mental & Behavioral Health", "Psiquiatría y Medicación"),
    "Substance Use & Recovery": (
        "Mental & Behavioral Health",
        "Uso de Sustancias y Recuperación",
    ),
    "Crisis Hotline": ("Mental & Behavioral Health", "Línea de Crisis"),
    # Domestic & Sexual Violence Services
    "Domestic Violence Services": (
        "Domestic & Sexual Violence Services",
        "Servicios de Violencia Doméstica",
    ),
    # Disability & Aging Services
    "Senior Programs": ("Disability & Aging Services", "Programas para Adultos Mayores"),
    "Disability Support Services": (
        "Disability & Aging Services",
        "Servicios de Apoyo por Discapacidad",
    ),
    # Family Support & Child Welfare
    "Parenting Education": ("Family Support & Child Welfare", "Educación para Padres"),
    "Family Counseling & Support": (
        "Family Support & Child Welfare",
        "Consejería y Apoyo Familiar",
    ),
    "Foster Care & Adoption": (
        "Family Support & Child Welfare",
        "Cuidado de Crianza y Adopción",
    ),
    "Child Safety & Advocacy": (
        "Family Support & Child Welfare",
        "Seguridad y Defensa del Menor",
    ),
    # Early Childhood & Child Care
    "Child Care & Early Education": (
        "Early Childhood & Child Care",
        "Cuidado Infantil y Educación Temprana",
    ),
    "Early Intervention": ("Early Childhood & Child Care", "Intervención Temprana"),
    # K-12 Academic Support
    "K-12 Schools": ("K-12 Academic Support", "Escuelas K-12"),
    "Special Education Services": (
        "K-12 Academic Support",
        "Servicios de Educación Especial",
    ),
    "Tutoring & Homework Help": ("K-12 Academic Support", "Tutoría y Ayuda con la Tarea"),
    "After-School Programs": (
        "K-12 Academic Support",
        "Programas Después de la Escuela",
    ),
    "STEM Programs": ("K-12 Academic Support", "Programas STEM"),
    # Youth Development & Mentoring
    "Youth Leadership & Civic Engagement": (
        "Youth Development & Mentoring",
        "Liderazgo Juvenil y Participación Cívica",
    ),
    "Mentoring": ("Youth Development & Mentoring", "Mentoría"),
    "Summer Camps & Programs": (
        "Youth Development & Mentoring",
        "Campamentos y Programas de Verano",
    ),
    "Youth Justice & Reentry": (
        "Youth Development & Mentoring",
        "Justicia Juvenil y Reintegración",
    ),
    "Youth Jobs & Job Training": (
        "Youth Development & Mentoring",
        "Empleo y Capacitación Juvenil",
    ),
    # College & Career Readiness
    "College Access & Financial Aid": (
        "College & Career Readiness",
        "Acceso Universitario y Ayuda Financiera",
    ),
    "Scholarships": ("College & Career Readiness", "Becas"),
    # Adult Education & ESL
    "ESOL & English Classes": ("Adult Education & ESL", "Clases de Inglés (ESOL)"),
    "Adult Education & GED": ("Adult Education & ESL", "Educación para Adultos y GED"),
    # Employment & Workforce Training
    "Job Placement & Career Services": (
        "Employment & Workforce Training",
        "Colocación Laboral y Servicios de Carrera",
    ),
    "Vocational & Trade Training": (
        "Employment & Workforce Training",
        "Capacitación Vocacional y Oficios",
    ),
    # Legal & Immigration
    "Immigration Legal Services": (
        "Legal & Immigration",
        "Servicios Legales de Inmigración",
    ),
    "Citizenship & Naturalization": (
        "Legal & Immigration",
        "Ciudadanía y Naturalización",
    ),
    "Civil Legal Aid": ("Legal & Immigration", "Asistencia Legal Civil"),
    "Interpretation & Translation": (
        "Legal & Immigration",
        "Interpretación y Traducción",
    ),
    # Housing & Economic Development
    "Affordable Housing Development": (
        "Housing & Economic Development",
        "Desarrollo de Vivienda Asequible",
    ),
    # Small Business & Nonprofit Support
    "Small Business & Entrepreneurship": (
        "Small Business & Nonprofit Support",
        "Pequeños Negocios y Emprendimiento",
    ),
    "Nonprofit Capacity Building": (
        "Small Business & Nonprofit Support",
        "Fortalecimiento de Organizaciones",
    ),
    "Volunteer Opportunities": (
        "Small Business & Nonprofit Support",
        "Oportunidades de Voluntariado",
    ),
    # Sports, Recreation & Fitness
    "Youth Sports & Recreation": (
        "Sports, Recreation & Fitness",
        "Deportes y Recreación Juvenil",
    ),
    "Sports & Fitness": ("Sports, Recreation & Fitness", "Deportes y Ejercicio"),
    "Boating & Waterfront Recreation": (
        "Sports, Recreation & Fitness",
        "Navegación y Recreación Acuática",
    ),
    # Arts & Creative Programs
    "Visual & Performing Arts": ("Arts & Creative Programs", "Artes Visuales y Escénicas"),
    "Music Programs": ("Arts & Creative Programs", "Programas de Música"),
    "Media, Film & Podcasting": ("Arts & Creative Programs", "Medios, Cine y Podcasts"),
    # Faith & Spiritual Services
    "Faith & Worship": ("Faith & Spiritual Services", "Fe y Culto"),
    # Environmental Programs
    "Environment & Sustainability": (
        "Environmental Programs",
        "Medio Ambiente y Sostenibilidad",
    ),
    # Civic & Government Services
    "Municipal & City Services": ("Civic & Government Services", "Servicios Municipales"),
    "Transportation Assistance": ("Civic & Government Services", "Ayuda con Transporte"),
    "Library & Technology Access": (
        "Civic & Government Services",
        "Biblioteca y Acceso Tecnológico",
    ),
    "Information & Referral": ("Civic & Government Services", "Información y Referencias"),
    "Community Organizing & Advocacy": (
        "Civic & Government Services",
        "Organización y Abogacía Comunitaria",
    ),
    "Disaster Relief & Preparedness": (
        "Civic & Government Services",
        "Ayuda y Preparación ante Desastres",
    ),
    # Community Events & Public Life
    "Cultural & Community Events": (
        "Community Events & Public Life",
        "Eventos Culturales y Comunitarios",
    ),
    "Neighborhood Improvement": (
        "Community Events & Public Life",
        "Mejoramiento del Vecindario",
    ),
}

# ── multi-category assignments ────────────────────────────────────────────────
# A raw tag's PRIMARY tag comes from the rules below — automatic, so orgs added
# later classify without hand-editing. Secondary tags live here, keyed by the
# exact raw string, because they are curated judgment rather than anything a
# regex could derive. Transcribed from the boss's draft `(also: …)` annotations.
#
# Deriving these from patterns would over-match badly: "Youth Sports Nutrition &
# Wellness (Health Kicks!)" would pick up Sports, Nutrition and Wellness at once
# whether or not that was intended.
ALSO: dict[str, list[str]] = {
    # food ↔ environment
    "community gardens": ["Environment & Sustainability"],
    "costello urban farm": ["Environment & Sustainability"],
    "farmer's market": ["Environment & Sustainability"],
    # food ↔ schools / residential care
    "student food services": ["K-12 Schools"],
    "nutrition services": ["Community Meals"],
    "daily meals and snacks": ["In-Home & Residential Care"],
    "summer eats": ["Summer Camps & Programs"],
    # health ↔ money
    "health insurance enrollment and cost of care assistance": [
        "Financial Coaching & Credit Counseling"
    ],
    "billing and financial counseling": ["Financial Coaching & Credit Counseling"],
    "health insurance navigation": ["Financial Coaching & Credit Counseling"],
    # health ↔ environment / arts
    "environmental health and environmental justice": ["Nutrition & Wellness"],
    "music therapy": ["Music Programs"],
    "expressive and nature-based therapies": ["Visual & Performing Arts"],
    "mindfulness and wellness practices workshops": ["Nutrition & Wellness"],
    # health ↔ emergency response
    "blood donation": ["Primary & Walk-In Care"],
    "basic life support training": ["Primary & Walk-In Care"],
    # legal ↔ health (hospital interpreters)
    "interpreter services": ["Primary & Walk-In Care"],
    "translation services": ["Primary & Walk-In Care"],
    # youth justice ↔ legal
    "juvenile justice services": ["Civil Legal Aid"],
    "restorative justice": ["Civil Legal Aid"],
    "support services for court-involved youth": ["Civil Legal Aid"],
    # domestic violence ↔ legal / crisis
    "domestic violence and crime victim legal advocacy": ["Civil Legal Aid"],
    "24-hour crisis hotline": ["Domestic Violence Services"],
    # job training ↔ the trade being taught
    "home health aide training": ["In-Home & Residential Care"],
    "nursing assistant training": ["In-Home & Residential Care"],
    "music production/sound engineering/podcasting (sisu beatz)": [
        "Vocational & Trade Training"
    ],
    "photography and videography apprenticeship": ["Vocational & Trade Training"],
    "screen printing & embroidery training (sisu prints)": ["Visual & Performing Arts"],
    "sewing and sustainable fashion apprenticeship": ["Visual & Performing Arts"],
    "free barber & cosmetology shop for youth (sisu cutz)": [
        "Vocational & Trade Training"
    ],
    "teen sailing & boathouse training (cit program)": ["Youth Jobs & Job Training"],
    # youth ↔ arts / sports
    "youth empowerment & creative arts network (movement city)": [
        "Visual & Performing Arts"
    ],
    "youth media production": ["Youth Leadership & Civic Engagement"],
    "youth sports nutrition & wellness (health kicks!)": ["Nutrition & Wellness"],
    # CORRECTION (see qa/categorization_review.md): the draft filed this under
    # Legal & Immigration, presumably on "At-Risk Youth".
    "youth basketball & life skills program for at-risk youth (sisu basketball)": [
        "Youth Leadership & Civic Engagement"
    ],
    "mommy and me music time": ["Music Programs"],
    # CORRECTION: the draft filed book clubs under Civic & Government Services.
    # The library association is real, so it keeps that tag and gains Arts.
    "book clubs": ["Visual & Performing Arts"],
    # disability cross-listing
    "special education and student support services": ["Disability Support Services"],
    "special education day school (anderson school)": ["Disability Support Services"],
    "special education services": ["Disability Support Services"],
    "services for the blind and print-disabled": ["Disability Support Services"],
    "website accessibility tools": ["Disability Support Services"],
    # aging
    "exercise classes": ["Senior Programs"],
    "community outings": ["Cultural & Community Events"],
    "respite care": ["Family Counseling & Support"],
    # faith cross-listing
    "celebrate recovery program": ["Faith & Worship"],
    "shuttle ministry transportation to church": ["Faith & Worship"],
    "bridge ministry outreach and essential supplies": ["Faith & Worship"],
    # civic / utilities
    "mbta reduced fares assistance": ["Public Benefits Assistance"],
    "water & energy utility services": ["Utility & Energy Bill Assistance"],
    "adopt-a-block community outreach": ["Neighborhood Improvement"],
    "childhood lead poisoning prevention": ["Child Safety & Advocacy"],
    "park cleanups": ["Neighborhood Improvement"],
    "urban tree planting program": ["Neighborhood Improvement"],
}

# ── exact overrides ───────────────────────────────────────────────────────────
# Raw tags whose keywords pull them to the wrong canonical tag. Checked before
# the pattern rules, matched case-insensitively on the whole string.
OVERRIDES = {
    # "Child Care Professional Training" is workforce training for adults,
    # not a child care service for families.
    "child care professional training and workforce development": "Vocational & Trade Training",
    # "Day Programs" here is adult disability day hab, not child care.
    "day programs": "Disability Support Services",
    # Youth-sports nutrition, not a nutrition service for the public.
    "youth sports nutrition & wellness (health kicks!)": "Youth Sports & Recreation",
    # Health-system HR, not a public workforce program.
    "staff recruitment and professional development": None,
    # Red Cross "International Services" is restoring family links after
    # conflict and disaster, not an immigration service.
    "international services": "Disaster Relief & Preparedness",
    # City of Lawrence's 911 / emergency management listing.
    "emergency services": "Disaster Relief & Preparedness",
    # MassHealth youth behavioral-health care coordination.
    "community service agency (csa) programs": "Mental Health Counseling",
    # Cristo Rey students work a corporate job to fund tuition.
    "corporate work study program": "Youth Jobs & Job Training",
    # Adult day health outing program.
    "community outings": "Senior Programs",
    # Clinician endorsement for the infant mental health workforce.
    "massaimh endorsement® consultation and support": "Nonprofit Capacity Building",
    "lp network": "Small Business & Entrepreneurship",
    "connections to external educational programs": "Information & Referral",
    "advisory services": "Information & Referral",
    # Adult job-training programs, not care delivered to a patient.
    "home health aide training": "Vocational & Trade Training",
    "nursing assistant training": "Vocational & Trade Training",
    # The college's own rentable event space, not rental housing help.
    "facilities usage & event rental": "Cultural & Community Events",
    # Groundwork Lawrence teaches cooking as nutrition education.
    "cooking classes": "Nutrition & Wellness",
    # The community college's culinary program runs a paying restaurant and a
    # catering business. Filing them under Community Meals would point someone
    # who is food-insecure at a place that charges them.
    "catering services": None,
    "restaurant dining services (public)": None,
    # An arts program, not a clinical mindfulness service.
    "mindful arts summer institute": "Visual & Performing Arts",
    # Bare "Education" is exactly the over-generic tag this taxonomy exists to
    # avoid. Both orgs carrying it have specific tags too, so drop it.
    "education": None,
    # Fragments the comma splitter leaves behind — prose, not services.
    "group": None,
    "student": None,
    "trainings": None,
    "groups and workshops": None,
    "cultural wealth": None,
    # "Access to physical, occupational, and speech therapies" splits into
    # three; the first two are meaningless alone, the third carries it.
    "access to physical": None,
    "occupational": None,
    "speech therapies": "In-Home & Residential Care",
}

# ── pattern rules ─────────────────────────────────────────────────────────────
# Ordered: FIRST match wins, so narrower patterns come before broader ones.
# Each raw tag maps to exactly one canonical tag — an org with ten raw tags
# typically lands on five or six canonical ones.
RULES: list[tuple[str, str]] = [
    # -- crisis first: a hotline tag must not be eaten by "counseling" --
    (r"crisis (hotline|lifeline)|suicide|24-hour crisis|samaritans", "Crisis Hotline"),
    # "bystander" belongs here, not under child safety: the only tag carrying it
    # is Jeanne Geiger Crisis Center's DV-prevention education.
    (r"domestic violence|intimate partner|sexual assault|crime victim|bystander", "Domestic Violence Services"),
    (r"child advocacy|children's safety|child safety", "Child Safety & Advocacy"),
    (r"restorative justice|juvenile justice|court-involved|truant", "Youth Justice & Reentry"),
    (r"disaster|home fire campaign|armed forces|blood donation|basic life support", "Disaster Relief & Preparedness"),
    (r"crisis stabilization", "Mental Health Counseling"),

    # -- food --
    (r"food pantry|pantry|soup kitchen|emergency food", "Food Pantry"),
    (r"community meal|soup meal|meal program|meal service|meal train|daily meals|student food|grab and go|summer eats", "Community Meals"),
    (r"mobile (food|pantry)|food distribution|food rescue|food access|food outreach|food assistance|food support|food resiliency|meal packaging|alleviate hunger|operation nourish|community market|farmer|community garden|urban farm|thanksgiving", "Food Distribution & Access"),
    (r"snap|wic ", "SNAP & WIC Enrollment"),
    (r"clothing|thrift|housewares|restore donations|back to school|school supply|styling|threads to go|pop-up shopping|barber|cosmetology|hair and beauty|mobile showers|laundry|holiday|evening of giving|blessing kit|essential goods|essential supplies|direct kindness packages", "Clothing & Essential Goods"),
    (r"diaper|breastfeeding|bottle feeding", "Diapers & Baby Supplies"),

    # -- housing --
    (r"emergency shelter|safe shelter|homeless", "Emergency Shelter"),
    (r"transitional housing|youth housing", "Transitional Housing"),
    (r"rental assistance|rental counseling|landlord|tenant|housing and foreclosure defense", "Rental Assistance & Tenant Rights"),
    (r"foreclosure", "Foreclosure Prevention"),
    (r"homebuyer|homeownership|first-time home", "Homebuyer Education"),
    (r"affordable housing|housing development|commercial real estate|property and asset management|community infrastructure", "Affordable Housing Development"),
    (r"home repair|home energy|weatheriz|lead poisoning", "Home Repair & Energy Efficiency"),
    (r"housing assistance|resident services", "Rental Assistance & Tenant Rights"),

    # -- health --
    (r"pediatric", "Pediatric Care"),
    (r"gynecolog|obstetric|prenatal|women's health|pregnancy support", "Women's Health & Prenatal Care"),
    (r"substance use|addiction|recovery support|celebrate recovery|outpatient addiction", "Substance Use & Recovery"),
    (r"psychiatric|psychopharmacolog|medication clinic", "Psychiatry & Medication"),
    # Physical/occupational/speech therapy is rehab, not counseling — this has
    # to precede the "therapy" rule below or it lands in Mental Health.
    (r"in-home|respite|residential (service|program)|geriatric|rehabilitation|medical monitoring|hospitalist|home health aide|nursing assistant|(physical|occupational|speech).{0,12}therap", "In-Home & Residential Care"),
    # \b on "mental health" — without it, "EnvironMENTAL HEALTH and
    # Environmental Justice" was filing under mental health counseling.
    (r"\bmental health|behavioral health|counseling \(individual|therap(y|ies)|tf-cbt|school counseling|iecmh|emotional support|mindfulness", "Mental Health Counseling"),
    (r"health insurance|cost of care|billing and financial counseling|patient portal|medical records", "Health Insurance Enrollment"),
    (r"pharmacy", "Pharmacy Services"),
    (r"nutrition|healthy lifestyle|health literacy|disease prevention|health education|public and social health", "Nutrition & Wellness"),
    (r"walk-in|internal medicine|laboratory|telehealth|student health|life and health|physician communication|health and family education|emergency care", "Primary & Walk-In Care"),

    # -- children & family --
    (r"early intervention|parents as teachers|healthy families", "Early Intervention"),
    (r"child care|childcare|day care|daycare|early childhood|early learning|pre-school|kindergarten program|head start|mommy and me", "Child Care & Early Education"),
    (r"foster|adoption|kinship|kid.s net|maff", "Foster Care & Adoption"),
    (r"parent education|parenting|family coaching|family partnership|family resource center|coordinated family|family & community engagement|family and community|strengthening families", "Parenting Education"),
    (r"family counseling|family support|child and family|individual and family|family strengthening|group counseling|equip individuals and families|general community & family support", "Family Counseling & Support"),

    # -- youth --
    (r"after[- ]school|after school|extended day|out-of-school|in school program|evening study|academic overtime|ydo kids|programs at phillips academy", "After-School Programs"),
    (r"summer camp|summer day camp|summer program|summer enrichment|summer intensive|camp and summer|sport & wellness camp|spring break", "Summer Camps & Programs"),
    (r"tutoring|homework help|academic support|academic excellence|academic tutoring|sat prep|rigorous academic|curriculum and instruction|academic program", "Tutoring & Homework Help"),
    (r"mentor", "Mentoring"),
    (r"youth (sport|basketball|soccer)|athletic|competitive youth|winter futsal|swim lesson|squash|crew and rowing|skills & training clinic|coaching certification|international tournament|female hoops|recreational/youth|recreation promotion", "Youth Sports & Recreation"),
    (r"youth (employment|workforce|vocational)|leadership & youth jobs|youthbuild|local job connection", "Youth Jobs & Job Training"),
    (r"teen center|youth board|youth development|empower youth|movement city|social justice collective|youth leadership|character & leadership|character development|leadership academy|youth empowerment|beyond resilient girls|women's empowerment|student leadership|national honor society|extra-curricular|community service (event|opportunit)|mlk day|adopt-a-block", "Youth Leadership & Civic Engagement"),

    # -- education --
    (r"esol|esl class|english language learner|english learner|multilingual learner", "ESOL & English Classes"),
    (r"ged|adult education|adult technical training|online adult|online learning", "Adult Education & GED"),
    (r"college|dual enrollment|financial aid|high school placement|post-secondary|graduate success|alumni", "College Access & Financial Aid"),
    (r"scholarship", "Scholarships"),
    # Scoped to schools. "Disability Support Services" and "Autism Services"
    # here belong to an adult provider, so they fall through to the Older
    # Adults & Disability rule below instead.
    (r"special education|student support services|website accessibility", "Special Education Services"),
    (r"stem|science & lab|health sciences program|business & computer technology", "STEM Programs"),
    (r"librar|technology access|physical and digital|material acquisition|career resource library|book club|blind and print-disabled", "Library & Technology Access"),
    (r"k-12|k1-8|charter public school|preparatory high school|general studies|humanities & communication|religious education|sacramental|campus ministry|faith formation|school safety", "K-12 Schools"),

    # -- jobs & money --
    (r"career exploration|career and technical|career & technical|vocational training|culinary & hospitality|construction & carpentry|screen printing|sewing|automotive repair|apprenticeship program|training & certification|workforce skill|human services & criminal justice", "Vocational & Trade Training"),
    (r"job placement|employment services|workforce development|workforce analysis|labor market|college & career planning|peer support for career|career", "Job Placement & Career Services"),
    (r"business|entrepreneur|venture loan|final pitch|kind business", "Small Business & Entrepreneurship"),
    (r"financial literacy|credit counseling|financial assistance|financial coaching|membership financial", "Financial Coaching & Credit Counseling"),
    (r"tax prep", "Tax Preparation"),
    (r"energy bill|liheap|utility service|water & energy", "Utility & Energy Bill Assistance"),
    (r"public benefit|consumer and tax legal|social work assistance", "Public Benefits Assistance"),

    # -- legal & immigration --
    (r"immigration|immigrant", "Immigration Legal Services"),
    (r"citizenship|naturalization", "Citizenship & Naturalization"),
    (r"legal|elder law|family law|advocacy services|consumer protection|advisory services", "Civil Legal Aid"),
    (r"interpreter|translation", "Interpretation & Translation"),

    # -- older adults & disability --
    (r"senior|older adult|elderly|cognitive stimulation", "Senior Programs"),
    (r"disabilit|special needs|autism", "Disability Support Services"),

    # -- arts & recreation --
    (r"music|songwriting|sound engineering|sisu beatz", "Music Programs"),
    (r"podcast|media production|videography|photography|film|in-hospital patient entertainment|livestream", "Media, Film & Podcasting"),
    (r"art gallery|visual art|performing arts|theater|playwriting|dance|creative writing|arts program|mindful arts|public art|student performance|entertainment|^arts\b", "Visual & Performing Arts"),
    (r"sailing|canoe|kayak|paddleboard|boat|rowing|waterfront", "Boating & Waterfront Recreation"),
    (r"fitness|sports|recreation|swim|exercise class", "Sports & Fitness"),
    (r"cultural|festival|juneteenth|public lecture|fun fest|community engagement workshop|know your city|special kindness|kindness happenings|facilities usage|event rental|team networking|celebration", "Cultural & Community Events"),

    # -- community --
    (r"advocacy|organizing|environmental justice|civic transparency|community development|community engagement", "Community Organizing & Advocacy"),
    (r"environment|watershed|water quality|tree planting|climate|sewer|park cleanup", "Environment & Sustainability"),
    (r"neighborhood|public space|community garden", "Neighborhood Improvement"),
    (r"volunteer", "Volunteer Opportunities"),
    (r"nonprofit|capacity building|board development|technical assistance|executive roundtable|grants|fiscal agent", "Nonprofit Capacity Building"),
    (r"city forms|municipal|voting|elections|local governance|online financial & regulatory|waste management|public works", "Municipal & City Services"),
    (r"transportation|bus route|mbta|shuttle|valet parking", "Transportation Assistance"),
    (r"worship|mass and confession|homilies|ministry|parish|spiritual|stations of the cross|baptism|formed|legacy society|faith formation|prayer line|cemetery", "Faith & Worship"),
    (r"referral|information assistance|community resources|community information|resource", "Information & Referral"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), tag) for p, tag in RULES]

# Fail fast on a typo in a rule's or ALSO entry's target rather than silently
# producing a tag that has no category and no Spanish label.
_unknown = {tag for _p, tag in RULES if tag not in TAGS}
_unknown |= {t for t in OVERRIDES.values() if t is not None and t not in TAGS}
_unknown |= {t for tags in ALSO.values() for t in tags if t not in TAGS}
if _unknown:
    raise ValueError(f"Rules reference canonical tags missing from TAGS: {sorted(_unknown)}")

_uncategorized = {t for t, (cat, _es) in TAGS.items() if cat not in CATEGORY_ORDER}
if _uncategorized:
    raise ValueError(f"Tags in a category not in CATEGORY_ORDER: {sorted(_uncategorized)}")


def canonicalize(raw_tag: str) -> str | None:
    """The single best canonical tag for a raw service string, or None.

    Rule order decides: first match wins, narrowest patterns first.
    """
    s = str(raw_tag).strip()
    if not s:
        return None

    key = s.lower()
    if key in OVERRIDES:
        return OVERRIDES[key]

    for pattern, tag in _COMPILED:
        if pattern.search(s):
            return tag
    return None


def canonicalize_all(raw_tag: str) -> list[str]:
    """Every canonical tag a raw service belongs under — primary plus ALSO.

    Multi-assignment comes from the boss's categorization: a community garden
    is genuinely both a food program and an environmental one, and someone
    browsing either category should find it.
    """
    primary = canonicalize(raw_tag)
    if primary is None:
        return []

    out = [primary]
    for extra in ALSO.get(str(raw_tag).strip().lower(), []):
        if extra not in out:
            out.append(extra)
    return out


def canonical_tags(raw_tags: list[str]) -> list[str]:
    """Canonical tags for one organization, de-duplicated, in category order."""
    found = {t for r in raw_tags for t in canonicalize_all(r)}
    return sorted(found, key=sort_key)


def sort_key(tag: str) -> tuple[int, str]:
    """Category order first, then alphabetical within the category."""
    category = TAGS[tag][0]
    return (CATEGORY_ORDER.index(category), tag)


# ── quick filters ─────────────────────────────────────────────────────────────
# One-tap shortcuts shown above the map for what people in crisis actually look
# for, so the common cases never require opening the sidebar and reading a list.
# Several chips cover more than one tag because the everyday word is broader
# than the taxonomy term ("Health Care" is both walk-in care and getting
# insured). The label keys resolve against T in app.py.
#
# Lives here rather than in app.py so the validator can import it without
# booting Streamlit.
QUICK_FILTERS: list[tuple[str, list[str]]] = [
    ("quick_food_pantry", ["Food Pantry"]),
    ("quick_meals", ["Community Meals"]),
    ("quick_shelter", ["Emergency Shelter"]),
    ("quick_housing", ["Rental Assistance & Tenant Rights", "Foreclosure Prevention"]),
    ("quick_health", ["Primary & Walk-In Care", "Health Insurance Enrollment"]),
    ("quick_immigration", ["Immigration Legal Services", "Citizenship & Naturalization"]),
    ("quick_childcare", ["Child Care & Early Education"]),
    ("quick_jobs", ["Job Placement & Career Services", "Vocational & Trade Training"]),
]

_bad_chips = {t for _k, tags in QUICK_FILTERS for t in tags if t not in TAGS}
if _bad_chips:
    raise ValueError(f"QUICK_FILTERS reference unknown tags: {sorted(_bad_chips)}")


def tag_label(tag: str, lang: str = "en") -> str:
    """Just the tag, translated — for the active-filter pills, where the
    category prefix would double the pill's width without adding meaning."""
    return TAGS[tag][1] if lang == "es" else tag


def display_label(tag: str, lang: str = "en") -> str:
    """Dropdown label: 'Category · Tag', so the list reads as grouped.

    Streamlit's multiselect has no option groups, so the category is carried
    in the label itself. It also makes the box's type-to-filter work as a
    category browse — typing "food" surfaces every food tag at once.
    """
    category, tag_es = TAGS[tag]
    if lang == "es":
        return f"{CATEGORY_ES[category]} · {tag_es}"
    return f"{category} · {tag}"


# ── the boss's category assignments ───────────────────────────────────────────
# data/service_categories.csv is generated from qa/boss_categorization_2026-09-08.md
# by scripts/parse_boss_categorization.py. It is the AUTHORITY on which category
# a raw service belongs to — the regex rules above decide the specific canonical
# tag, but not the category, for any service the draft names.
#
# The draft was written against a 410-tag snapshot and the CSV now holds 485, so
# ~80 services have no assignment in it. Those fall back to the category of
# whatever canonical tag the rules produce, and are listed by the parser script
# so they can be assigned properly.
_CATEGORIES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "service_categories.csv")


def _normalize(tag: str) -> str:
    """Fold typographic punctuation so the draft and the CSV compare equal."""
    return (
        str(tag)
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .strip()
    )


def _load_assigned() -> dict[str, list[str]]:
    if not os.path.exists(_CATEGORIES_CSV):
        return {}
    out: dict[str, list[str]] = {}
    with open(_CATEGORIES_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cats = [c.strip() for c in row["Categories"].split(";") if c.strip()]
            if cats:
                out[_normalize(row["RawService"]).lower()] = cats
    return out


ASSIGNED_CATEGORIES = _load_assigned()

_bad = {c for cats in ASSIGNED_CATEGORIES.values() for c in cats if c not in CATEGORY_ORDER}
if _bad:
    raise ValueError(f"service_categories.csv names unknown categories: {sorted(_bad)}")


def categories_for(raw_tag: str) -> list[str]:
    """Every category one raw service belongs to.

    The draft's assignment wins where it has one. Otherwise the category is
    inferred from the canonical tags the rules produce, so a service added
    after the draft was written still lands somewhere sensible.
    """
    assigned = ASSIGNED_CATEGORIES.get(_normalize(raw_tag).lower())
    if assigned:
        return list(assigned)
    return list(dict.fromkeys(TAGS[t][0] for t in canonicalize_all(raw_tag)))


def org_categories(raw_tags: list[str]) -> list[str]:
    """Categories for one organization, de-duplicated, in CATEGORY_ORDER order."""
    found = {c for r in raw_tags for c in categories_for(r)}
    return sorted(found, key=CATEGORY_ORDER.index)


def category_label(category: str, lang: str = "en") -> str:
    return CATEGORY_ES.get(category, category) if lang == "es" else category
