from django.http import HttpResponseRedirect, HttpResponse, StreamingHttpResponse, HttpRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.db.models import Q
from django.db import transaction
from django.contrib import messages
from courselib.auth import requires_course_staff_by_slug, requires_course_instr_by_slug, requires_role, has_role, \
    is_course_staff_by_slug, is_course_instr_by_slug, user_passes_test, \
    ForbiddenResponse, NotFoundResponse, HttpError
from courselib.branding import help_email
from django.contrib.auth.decorators import login_required
from ta.models import TUG, Skill, SkillLevel, TAApplication, TAPosting, TAContract, TACourse, CoursePreference, \
    CampusPreference, CourseDescription, \
    CAMPUS_CHOICES, PREFERENCE_CHOICES, LEVEL_CHOICES, PREFERENCES, LEVELS, LAB_BONUS, LAB_BONUS_DECIMAL, HOURS_PER_BU, \
    HOLIDAY_HOURS_PER_BU, LAB_PREP_HOURS, TAContractEmailText, TAWorkloadReview, TAEvaluation
from tacontracts.models import TACourse as NewTACourse
from ra.models import Account
from grad.models import GradStudent, STATUS_REAL_PROGRAM, STATUS_ACTIVE
from dashboard.models import NewsItem
from coredata.models import Member, Role, CourseOffering, Person, Semester, CAMPUSES, CombinedOffering, SemesterWeek
from coredata.queries import more_personal_info, SIMSProblem, ensure_person_from_userid
from grad.models import GradStatus, GradStudent, Supervisor
from ta.forms import TUGForm, TAApplicationForm, TAContractForm, TAAcceptanceForm, CoursePreferenceForm, \
    TAPostingForm, TAPostingBUForm, BUFormSet, TACourseForm, BaseTACourseFormSet, AssignBUForm, TAContactForm, \
    CourseDescriptionForm, LabelledHidden, NewTAContractForm, TAContractEmailTextForm, TAWorkloadReviewForm, \
    TAEvaluationForm, TAEvaluationFormbyTA
from advisornotes.forms import StudentSearchForm
from log.models import LogEntry
from dashboard.letters import ta_form, ta_forms, tug_form, taworkload_form, ta_evaluation_form
from django.forms.models import inlineformset_factory
from django.forms.formsets import formset_factory
from django.core.paginator import Paginator, EmptyPage, InvalidPage
import datetime, decimal, locale 
import csv
from ta.templatetags import ta_display
import json
from . import bu_rules
import iso8601;
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from collections import OrderedDict
from formtools.wizard.views import SessionWizardView
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.template.loader import get_template
from datetime import date
from tacontracts.models import HiringSemester

# Template 
# 2022-2025 TUG: view_tug_2022-2025.html 
# old version before 2022: view_tug.html     

# prod > 1247
TUG_FORMAT_CUTOFF = '1247'

locale.setlocale( locale.LC_ALL, 'en_CA.UTF-8' ) #fiddle with this if you cant get the following function to work
def _format_currency(i):
    """used to properly format money"""
    return locale.currency(float(i), grouping=True)


def _create_news(person, url, from_user, accept_deadline, total_bu ):

    # attempt to e-mail the student's supervisor
    gradstudents = GradStudent.objects.filter(person=person, current_status__in=STATUS_ACTIVE)
    if len(gradstudents) > 0:
        gradstudent = gradstudents[0]
        # See if we can find a supervisor to notify.  The student shouldn't have Senior, CoSenior, and Potential
        #  supervisors, so we'll just get all of those and grab the first one.
        supervisors = Supervisor.objects.filter(student=gradstudent, supervisor_type__in=['SEN', 'COS', 'POT'],
                                                removed=False, external__isnull=True)
        if len(supervisors) > 0:
            supervisor = supervisors[0].supervisor
            n = NewsItem(user=supervisor,
                         source_app="ta_contract",
                         title="TA Contract Offer for %s" % person,
                         author=from_user,
                         content="Your student %s has been offered a TA contract with %s BUs." % (person, total_bu)
                         )
            n.save()

    n = NewsItem(user=person, source_app="ta_contract", title="TA Contract Offer for %s" % (person),
                 url=url, author=from_user, content="You have been offered a TA contract. You must log in and accept or reject it by %s."%(accept_deadline))
    n.save()


def _is_admin_by_slug(request, course_slug, **kwargs):
    offering = get_object_or_404(CourseOffering, slug=course_slug)
    roles = Role.objects_fresh.filter(person__userid=request.user.username, role='ADMN', unit=offering.owner).count() \
            + Role.objects_fresh.filter(person__userid=request.user.username, role='TAAD', unit=offering.owner).count()
    return roles > 0

def _requires_course_staff_or_admin_by_slug(function=None, login_url=None):
    """
    Allows access if user is a staff member (instructor, TA, approver) from course indicated by 'course_slug'
    *or* if they are the departmental admin for the course's department
    """
    def test_func(request, **kwargs):
        return is_course_staff_by_slug(request, expires=False, **kwargs) or _is_admin_by_slug(request, **kwargs)
    actual_decorator = user_passes_test(test_func, login_url=login_url)
    if function:
        return actual_decorator(function)
    else:
        return actual_decorator

def _requires_course_instr_or_admin_by_slug(function=None, login_url=None):
    """
    Allows access if user is an instructor from course indicated by 'course_slug'
    *or* if they are the departmental admin for the course's department
    """
    def test_func(request, **kwargs):
        return is_course_instr_by_slug(request, **kwargs) or _is_admin_by_slug(request, **kwargs)
    actual_decorator = user_passes_test(test_func, login_url=login_url)
    if function:
        return actual_decorator(function)
    else:
        return actual_decorator

@requires_role('TAAD')
def tugs_csv_download(request, semester_name=None):
    """
    Download of all TUG data for a semester for admins
    """
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()

    # get all the tugs appearing on the tugs page for this semester
    course_ids = [o.id for o in CourseOffering.objects.filter(owner__in=request.units, semester=semester)]
    tas = Member.objects.filter(offering_id__in=course_ids, role="TA")
    tugs = TUG.objects.filter(member__in=set(tas), draft=False).select_related('member__person')

    if semester.name >= TUG_FORMAT_CUTOFF:
        dr_descs = ['Preparation for labs/tutorials/workshops', 'Attendance at orientation and planning/coordinating meetings with instructor',
                     'Preparation for lectures', 'Attendance at lectures, including breakout groups', 'Support classroom course delivery, including technical support', 
                     'Attendance at labs/tutorials/workshops', 'Leading discussions', 'Office hours/student consultation', 'Electronic communication', 'Grading', 
                     'Quiz preparation/assist in exam preparation/Invigilation of exams', 'Statutory Holiday Compensation', 'Other 1', 'Other 2']
        dr_configs = ['prep', 'meetings', 'prep_lectures', 'lectures', 'support', 'tutorials', 'leading', 'office_hours', 'e_communication', 'grading', 'test_prep', 'holiday', 'other1', 'other2']
    else:
        dr_descs = ['Preparation for labs/tutorials', 'Attendance at planning/coordinating meetings with instructor',
                'Attendance at lectures', 'Attendance at labs/tutorials',
                'Office hours/student consultation/electronic communication', 'Grading',
                'Quiz preparation/assist in exam preparation/Invigilation of exams', 'Statutory Holiday Compensation', 
                'Other 1', 'Other 2']
        dr_configs = ['prep', 'meetings', 'lectures', 'tutorials', 'office_hours', 'grading', 'test_prep', 'holiday', 'other1', 'other2']
    dr_column_names = [f"DR{i+1} Hrs/Sem ({dr})" for i, dr in enumerate(dr_descs)]

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="tugs(%s)-%s.csv"' % (semester.name, datetime.datetime.now().strftime('%Y%m%d'))
    writer = csv.writer(response)
    writer.writerow(['TA Name', 'TA ID', 'Instructor', 'Course Number', 'Course Section', 'Course Term', 'BUs Without Prep', 'BUs With Prep', 'Maximum Hours']
                    + dr_column_names + ['Required Total Hours', 'Link to TUG'])
    
    for tug in tugs:
        person = tug.member.person
        course_offering = tug.member.offering
        instructors = '; '.join(p.sortname_pref() for p in course_offering.instructors())
        dr_row_values = [tug.config[vn]['total'] for vn in dr_configs]
        iterable_fields = [(_, params) for _, params in tug.config.items() if hasattr(params, '__iter__') ]
        total_hours = round(sum(decimal.Decimal(params.get('total',0)) for _, params in iterable_fields if params.get('total',0) is not None), 2)
        url = request.build_absolute_uri(reverse('offering:view_tug', kwargs={'course_slug': course_offering.slug, 'userid': person.userid}))

        contract_info = __get_contract_info(tug.member)
        if contract_info:
            bu = contract_info.bu
            total_bu = contract_info.total_bu
            max_hours = contract_info.hours
        else:
            bu = tug.base_units
            total_bu = tug.base_units + LAB_BONUS_DECIMAL
            max_hours = tug.base_units * HOURS_PER_BU
        course_number = course_offering.subject + " " + course_offering.number

        writer.writerow([person, person.emplid, instructors, course_number, course_offering.section, course_offering.semester.label(), bu, total_bu, max_hours] + dr_row_values + [total_hours, url])
    return response




@login_required
def all_tugs_admin(request, semester_name=None):
    """
    View for admins to see all TUGS and instructors to manage theirs
    """
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()

    admin = has_role('TAAD', request)
    instr_members = Member.objects.filter(person__userid=request.user.username, role='INST') \
            .exclude(offering__component='CAN').select_related('offering')
    if not admin and not instr_members:
        return ForbiddenResponse(request)

    admin_tas = set()
    instr_tas = set()
    if admin:
        courses = CourseOffering.objects.filter(owner__in=request.units, semester=semester)
        course_ids = [o.id for o in courses]
        admin_tas = Member.objects.filter(offering_id__in=course_ids, role="TA").select_related('offering__semester', 'person', 'offering__owner')
        admin_tas = set(admin_tas)

    if instr_members:
        # allow all instructors to see the page, but only populate with current semester's TAs
        instr_members = instr_members.filter(offering__semester=semester)
        offering_ids = set(m.offering_id for m in instr_members)
        instr_tas = Member.objects.filter(offering_id__in=offering_ids, role='TA').select_related('offering__semester', 'offering__owner')
        instr_tas = set(instr_tas)

    all_tas = admin_tas | instr_tas

    # build list of all instructors here, to save two queries per course later
    offering_ids = set(m.offering_id for m in all_tas)
    all_instr = Member.objects.filter(role='INST', offering_id__in=offering_ids).select_related('person', 'offering')
    for inst in all_instr:
        for ta in (m for m in all_tas if m.offering_id == inst.offering_id):
            if not hasattr(ta, 'instructors'):
                ta.instructors = []
            ta.instructors.append(inst)

    all_tugs = TUG.objects.filter(member__in=all_tas).select_related('member__person')
    tug_dict = dict((tug.member_id, tug) for tug in all_tugs)

    all_taworkloads = TAWorkloadReview.objects.filter(member__in=all_tas).select_related('member__person')
    taworkload_dict = dict((taworkload.member_id, taworkload) for taworkload in all_taworkloads)

    sw5 = SemesterWeek.objects.get(semester=semester, week=1).monday + datetime.timedelta(days=28) # week 5 - 28 days
    is_wr_visible = datetime.date.today() >= sw5

    all_taevals = TAEvaluation.objects.filter(member__in=all_tas).select_related('member__person', 'member__offering')
    taeval_dict = dict((taeval.member_id, taeval) for taeval in all_taevals)

    if request.method == "POST" and 'send_instr_reminders' in request.POST:
        # POST request to send reminders to Instructors
        ccount = _send_reminders_to_instr(request, semester_name, all_taevals)          
        if ccount > 1:
            messages.success(request, "Successfully sent %s reminders." % ccount)
        elif ccount > 0:
            messages.success(request, "Successfully sent reminder.")

    if request.method == "POST" and 'sendrelease' in request.POST:
        # POST request to send release to TA
        ccount = _send_release_to_tas(request, semester_name, all_taevals)          
        if ccount > 1:
            messages.success(request, "Successfully sent %s reminders." % ccount)
        elif ccount > 0:
            messages.success(request, "Successfully sent reminder.")

    if request.method == "POST" and 'sendreminders' in request.POST:
        # POST request to send reminders to TA
        ccount = _send_reminders_to_tas(request, semester_name, all_taevals)          
        if ccount > 1:
            messages.success(request, "Successfully sent %s reminders." % ccount)
        elif ccount > 0:
            messages.success(request, "Successfully sent reminder.")

    if request.method == "POST" and 'sendincomplete' in request.POST:
        # POST request to send incomplete evals to admin
        ccount = _send_incomplete_to_admin(request, semester_name, all_taevals)  
        if ccount > 1:
            messages.success(request, "Successfully sent %s incompleted TA evals." % ccount)
        elif ccount > 0:
            messages.success(request, "Successfully sent an incompleted TA eval.")


    try:
        nextsemstart = semester.next_semester().start
        is_past_nextsemstart = date.today() > nextsemstart
        nextsem7day = nextsemstart + datetime.timedelta(days=7) 
        nextsem14day = nextsemstart + datetime.timedelta(days=14) 
        nextsem28day = nextsemstart + datetime.timedelta(days=28) 
        is_cutoff = date.today() > nextsem28day
             
    except:
        nextsemstart = None
        is_past_nextsemstart = False
        nextsem7day = None
        nextsem14day = None
        nextsem28day = None
        is_cutoff = None

    tas_with_tugs = [
        {
            'ta': ta,
            'tug': tug_dict.get(ta.id, None),
            'is_instr': ta in instr_tas,
            'taworkload': taworkload_dict.get(ta.id, None),
            'taeval': taeval_dict.get(ta.id, None)
        }
        for ta in all_tas]

    context = {
            'admin': admin,
            'semester': semester,
            'tas_with_tugs': tas_with_tugs,
            'is_wr_visible': is_wr_visible,
            'nextsemstart': nextsemstart,
            'nextsem7day': nextsem7day,
            'nextsem14day': nextsem14day,
            'nextsem28day': nextsem28day,
            'is_past_nextsemstart': is_past_nextsemstart,
            'is_cutoff': is_cutoff
            }

    return render(request, 'ta/all_tugs_admin.html', context)

def _send_reminders_to_instr(request, semester_name=None, all_taevals=None):
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()
    
    if all_taevals is None:
        all_tas = set()
        courses = CourseOffering.objects.filter(owner__in=request.units, semester=semester)
        course_ids = [o.id for o in courses]
        all_tas = Member.objects.filter(offering_id__in=course_ids, role="TA").select_related('offering__semester', 'person')
        all_tas = set(all_tas)
    
        all_taevals = TAEvaluation.objects.filter(member__in=all_tas, draft=True).select_related('member__person', 'member__offering')

    ccount = 0
    for taeval in all_taevals:
        evalid = 'taeval_%s' % taeval.id        
        if evalid in request.POST and taeval.draft:                            
            from_email = settings.DEFAULT_FROM_EMAIL            
            # Send email notification to each instructor            
            subject = 'You have a draft TA Evaluation for your TA %s. Please review and submit it.' % taeval.member.person
            plaintext = get_template('ta/emails/notify_draft_ta_eval_for_instructor.txt')
            url = settings.BASE_ABS_URL + reverse('offering:edit_ta_evaluation_wizard', kwargs={'course_slug': taeval.member.offering.slug, 'userid': taeval.member.person.userid})
            email_context = {'person': taeval.member.person, 'offering': taeval.member.offering, 'url': url, 'instructor': taeval.member.offering.instructors_str()}
    
            instructors = Member.objects.filter(role='INST', offering=taeval.member.offering) 
            instructor_email_list = []   
            for member in instructors:                
                instructor_email_list.append(member.person.email())    
            msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context),
                                         from_email=from_email, to=instructor_email_list, headers={'X-coursys-topic': 'ta'})
            msg.send()
            l = LogEntry(userid='sysadmin',
                description=("manually email notification to instructor %s for drafted Eval for %s %s") % (
                    taeval.member.offering.instructors_str(), taeval.member.offering, taeval.member.person),
                    related_object=taeval)
            l.save()
            ccount += 1            

    return ccount

def _send_release_to_tas(request, semester_name=None, all_taevals=None):
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()
    
    if all_taevals is None:
        all_tas = set()
        courses = CourseOffering.objects.filter(owner__in=request.units, semester=semester)
        course_ids = [o.id for o in courses]
        all_tas = Member.objects.filter(offering_id__in=course_ids, role="TA").select_related('offering__semester', 'person')
        all_tas = set(all_tas)
    
        all_taevals = TAEvaluation.objects.filter(member__in=all_tas, draft=False, ta_signdate=None).select_related('member__person', 'member__offering')

    ccount = 0
    for taeval in all_taevals:
        evalid = 'taeval_%s' % taeval.id        
        if evalid in request.POST and not taeval.draft and taeval.ta_signdate is None:                            
            ta_edit_url = reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': taeval.member.offering.slug, 'userid': taeval.member.person.userid})
            n = NewsItem(user=taeval.member.person, source_app="ta_evaluation", title="Your TA Evaluation Form is created. Please review and provide your comment.",
            url=ta_edit_url, author=None, content="Your TA Evaluation Form is created. Please review and provide your comment.")
            n.save()
            taeval.config['released'] = True
            taeval.save()
            l = LogEntry(userid='sysadmin',
                        description=("manually TA Eval Release to TA %s for %s") % (
                        taeval.member.person, semester.name),
                        related_object=taeval)
            l.save()            
            ccount += 1            

    return ccount

def _send_reminders_to_tas(request, semester_name=None, all_taevals=None):
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()
    
    if all_taevals is None:
        all_tas = set()
        courses = CourseOffering.objects.filter(owner__in=request.units, semester=semester)
        course_ids = [o.id for o in courses]
        all_tas = Member.objects.filter(offering_id__in=course_ids, role="TA").select_related('offering__semester', 'person')
        all_tas = set(all_tas)
    
        all_taevals = TAEvaluation.objects.filter(member__in=all_tas, draft=False, ta_signdate=None).select_related('member__person', 'member__offering')

    ccount = 0
    for taeval in all_taevals:
        evalid = 'taeval_%s' % taeval.id        
        if evalid in request.POST and not taeval.draft and taeval.ta_signdate is None:                            
            ta_edit_url = reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': taeval.member.offering.slug, 'userid': taeval.member.person.userid})
            n = NewsItem(user=taeval.member.person, source_app="ta_evaluation", title="Your have not yet completed the section E on your TA Evaluation.",
            url=ta_edit_url, author=None, content="Your have not yet completed the section E on your TA Evaluation. Please review and provide your comment.")
            n.save()

            taeval.config['reminded'] = True
            taeval.save()
            l = LogEntry(userid='sysadmin',
                        description=("manually TA Eval Reminder to TA %s for %s") % (
                        taeval.member.person, semester.name),
                        related_object=taeval)
            l.save() 

            ccount += 1            

    return ccount

def _send_incomplete_to_admin(request, semester_name=None, all_taevals=None):
    if semester_name:
        semester = get_object_or_404(Semester, name=semester_name)
    else:
        semester = Semester.current()
    
    if all_taevals is None:
        all_tas = set()
        courses = CourseOffering.objects.filter(owner__in=request.units, semester=semester)
        course_ids = [o.id for o in courses]
        all_tas = Member.objects.filter(offering_id__in=course_ids, role="TA").select_related('offering__semester', 'person')
        all_tas = set(all_tas)
    
        all_taevals = TAEvaluation.objects.filter(member__in=all_tas, draft=False, ta_signdate=None).select_related('member__person', 'member__offering')

    ccount = 0
    for taeval in all_taevals:
        evalid = 'taeval_%s' % taeval.id
        if evalid in request.POST and not taeval.draft and taeval.ta_signdate is None:
            _send_notify_to_admin(taeval, 'incomplete')
            l = LogEntry(userid='sysadmin',
                    description=("manually sending incomplete TA Eval for %s on %s") % (
                    taeval.member.person, semester.name),
                    related_object=taeval)
            l.save()            
            ccount += 1            

    return ccount

def __get_contract_info(member):
    """
    Find TUG-related information about this contract (if we can)
    """
    crses = list(TACourse.objects.filter(course=member.offering, contract__application__person=member.person).exclude(contract__status='CAN')) \
        + list(NewTACourse.objects.filter(course=member.offering, contract__person=member.person).exclude(contract__status='CAN'))
    if crses:
        return crses[0]
    else:
        return None

@_requires_course_staff_or_admin_by_slug
def view_tug(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"
    
    #If the currently logged in user is a TA for the course and is viewing a TUG for another TA, show forbidden message
    if curr_user_role=="TA" and not userid==request.user.username: 
        return ForbiddenResponse(request)
    else:
        tug = get_object_or_404(TUG, member=member)
        if curr_user_role=="TA" and tug.draft: 
            return ForbiddenResponse(request)                
        iterable_fields = [(_, params) for _, params in tug.config.items() if hasattr(params, '__iter__') ]
        total_hours = sum(decimal.Decimal(params.get('total',0)) for _, params in iterable_fields if params.get('total',0) is not None)
        total_hours = round(total_hours, 2)

        contract_info = __get_contract_info(member)
        if contract_info:
            bu = contract_info.bu
            has_lab_or_tut = contract_info.has_labtut()
            lab_bonus_decimal = contract_info.prep_bu
            holiday_hours_per_bu = contract_info.holiday_hours_per_bu
            hours_per_bu = HOURS_PER_BU
            total_bu = contract_info.total_bu
            max_hours = contract_info.hours
        else:
            bu = tug.base_units
            has_lab_or_tut = course.labtas()
            lab_bonus_decimal = LAB_BONUS_DECIMAL
            holiday_hours_per_bu = HOLIDAY_HOURS_PER_BU
            hours_per_bu = HOURS_PER_BU
            total_bu = tug.base_units + LAB_BONUS_DECIMAL
            max_hours = tug.base_units * HOURS_PER_BU

        context = {'tug': tug, 
                'ta':member.person, 
                'course':course, 
                'bu': bu,
                'max_hours': max_hours, 
                'total_hours':total_hours,
                'user_role': curr_user_role,
                'has_lab_or_tut': has_lab_or_tut,
                'holiday_hours_per_bu': holiday_hours_per_bu,
                'lab_bonus': lab_bonus_decimal,
                'lab_bonus_4': lab_bonus_decimal+4,
                'hours_per_bu': hours_per_bu,
                'lab_bonus_hours': lab_bonus_decimal*hours_per_bu,
                'hours_per_bu': hours_per_bu,
                'holiday_hours_per_bu': holiday_hours_per_bu,
                'total_bu': total_bu,
                'draft': tug.draft,
                }
        

        if course.semester.name >= TUG_FORMAT_CUTOFF:
            template_file = 'ta/view_tug_2022-2025.html'
        else:
            template_file = 'ta/view_tug.html'
        return render(request, template_file,context)


@_requires_course_instr_or_admin_by_slug
def new_tug(request, course_slug, userid):
    return _edit_tug(request, course_slug, userid)

@_requires_course_instr_or_admin_by_slug
def edit_tug(request, course_slug, userid):
    tug = get_object_or_404(TUG, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')
    return _edit_tug(request, course_slug, userid, tug=tug)

@transaction.atomic
def _edit_tug(request, course_slug, userid, tug=None):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role='TA')

    contract_info = __get_contract_info(member)
    if contract_info:
        has_lab_or_tut = course.labtas() or contract_info.has_labtut()
        prep_min = contract_info.min_tug_prep
        bu = contract_info.bu
        lab_bonus = contract_info.prep_bu
        hours_per_bu = contract_info.hours_per_bu
        holiday_hours = contract_info.holiday_hours
        prep_bu = contract_info.prep_bu
    else:
        has_lab_or_tut = course.labtas()
        prep_min = LAB_PREP_HOURS if has_lab_or_tut else 0
        bu = member.bu()
        if not bu and tug:
            bu = tug.base_units
        lab_bonus = LAB_BONUS_DECIMAL
        hours_per_bu = HOURS_PER_BU
        holiday_hours = bu * HOLIDAY_HOURS_PER_BU
        prep_bu = 0

    if not tug:
        tug = TUG(member=member)
        draft = True
    else:
        draft = tug.draft        

    if course.semester.name >= TUG_FORMAT_CUTOFF:
        new_format = True
    else:
        new_format = False

    if request.method == "POST":
        form = TUGForm(instance=tug, data=request.POST, offering=course, userid=userid, enforced_prep_min=prep_min, new_format=new_format)
        
        if form.is_valid():            
            tug = form.save(False)
            if 'save_draft' in request.POST:
                tug.draft = True
                tug.save(newsitem=False)
            else:
                tug.draft = False
                tug.save(newsitem_author=Person.objects.get(userid=request.user.username))

            if not tug.draft: 
                contract_info = __get_contract_info(tug.member)               
                tug = get_object_or_404(TUG, member=member)
                _email_tug(tug, contract_info)
            return HttpResponseRedirect(reverse('offering:view_tug', args=(course.slug, userid)))
    else:
        form = TUGForm(instance=tug, offering=course, userid=userid, enforced_prep_min=prep_min, initial={
            'holiday':{'total': holiday_hours},
            'prep':{'total': prep_min or ''},
            'base_units': bu}, new_format=new_format)
        if prep_bu:
            form.fields['base_units'].help_text = \
                ('x %s = Maxinum Hours: %s (+ %s base units not assignable because of labs/tutorials)' %
                    (hours_per_bu, bu * hours_per_bu, prep_bu,))
        else:
            form.fields['base_units'].help_text = 'x %s = Maxinum Hours: %s' % (hours_per_bu, bu * hours_per_bu)

    if member.bu():
        # we know BUs from the TA application: don't allow editing
        form.fields['base_units'].widget = LabelledHidden()
        form.subforms['holiday'].fields['total'].widget = LabelledHidden()
        form.subforms['holiday'].fields['weekly'].widget = LabelledHidden()

    #/ta
    try:
        posting = TAPosting.objects.get(semester=course.semester, unit=course.owner)
    except:
        posting = None
    if posting: 
        tssu_link = posting.tssu_link()
    else:  
        tssu_link = 'https://www.sfu.ca/human-resources/tssu.html'

    context = {'ta':member.person,
               'course':course,
               'form':form,
               'userid':userid,
               'LAB_BONUS': lab_bonus,
               'LAB_BONUS_4': lab_bonus+4, # used in the help text
               'HOURS_PER_BU': hours_per_bu,
               'HOLIDAY_HOURS_PER_BU': HOLIDAY_HOURS_PER_BU,
               'tssu_link': tssu_link,
               'draft': draft,                
               }
    return render(request,'ta/edit_tug.html',context)

@_requires_course_staff_or_admin_by_slug
def download_tug(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"

    #If the currently logged in user is a TA for the course and is viewing a TUG for another TA, show forbidden message
    if curr_user_role=="TA" and not userid==request.user.username: 
        return ForbiddenResponse(request)
    else:
        tug = get_object_or_404(TUG, member=member)
        contract_info = __get_contract_info(tug.member)    
        if tug.member.offering.semester.name >= TUG_FORMAT_CUTOFF:
            new_format = True
        else:
            new_format = False

        response = HttpResponse(content_type="application/pdf")        
        filename = "%s_%s_%s_TUG_%s.pdf" % (tug.member.person.last_name, tug.member.person.first_name , tug.member.person.emplid, course_slug)
        response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
        tug_form(tug, contract_info, new_format, response)
        return response

def _email_tug(tug, contract_info):         
    # Email TA and Admin when instructor submit the TUG     
    subject = "TUG for %s (%s) in %s in %s" % (tug.member.person.sortname(), tug.member.person.emplid,tug.member.offering.name(), tug.member.offering.semester )
    plaintext = get_template('ta/emails/notify_tug.txt')
    posting = TAPosting.objects.filter(semester=tug.member.offering.semester, unit=tug.member.offering.owner).first()
    email_context = {'person': tug.member.person, 'posting': posting, 'offering': tug.member.offering}
    from_email = settings.DEFAULT_FROM_EMAIL
    response = HttpResponse(content_type="application/pdf")

    if tug.member.offering.semester.name >= TUG_FORMAT_CUTOFF:
        new_format = True
    else:
        new_format = False

    tug_form(tug, contract_info, new_format, response)

    # send to TA themselves
    to_email = []
    to_email.append(tug.member.person.email())

    # send to TA admin
    #/ta
    try:
        posting = TAPosting.objects.get(semester=tug.member.offering.semester, unit=tug.member.offering.owner)
    except:
        posting = None
    if posting: 
        to_email.append(posting.contact().email())

    #/tacontracts
    try: 
        hiring_semester = HiringSemester.objects.filter(semester=tug.member.offering.semester, unit=tug.member.offering.owner).first()
    except: 
        hiring_semester = None
    if hiring_semester:
        to_email.append(hiring_semester.contact)

    msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context),
                    from_email=from_email, to=to_email, headers={'X-coursys-topic': 'ta'})
    msg.attach(('%s_%s_%s_%s_TUG.pdf' % (tug.member.person.last_name, tug.member.person.first_name, tug.member.person.emplid, tug.member.offering.semester.slugform())), response.getvalue(),
                   'application/pdf')
    msg.send()

@_requires_course_staff_or_admin_by_slug
def view_ta_workload(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")  
        
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"
    
    #If the currently logged in user is a TA for the course and is viewing a TUG for another TA, show forbidden message
    if curr_user_role=="TA" and not userid==request.user.username: 
        return ForbiddenResponse(request)
    else:
        tug = get_object_or_404(TUG, member=member)        
        taworkload = get_object_or_404(TAWorkloadReview, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')
        
         
        context = {'tug': tug, 
                'ta':member.person, 
                'course':course,
                'taworkload': taworkload,
                'user_role': curr_user_role,
                }
        return render(request, 'ta/view_taworkload.html',context)


@_requires_course_instr_or_admin_by_slug
def new_ta_workload(request, course_slug, userid):
    return _edit_ta_workload(request, course_slug, userid)

@_requires_course_instr_or_admin_by_slug
def edit_ta_workload(request, course_slug, userid):
    taworkload = get_object_or_404(TAWorkloadReview, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')
    return _edit_ta_workload(request, course_slug, userid, taworkload=taworkload)

@transaction.atomic
def _edit_ta_workload(request, course_slug, userid, taworkload=None):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role='TA')
    #tug = get_object_or_404(TUG, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')
    tug = get_object_or_404(TUG, member=member)

    if not taworkload:
        taworkload = TAWorkloadReview(member=member)
        description = "Create Workload Review for TA %s "  % userid
        ta_workloadreviewform = TAWorkloadReviewForm(request.POST or None, instance=taworkload, initial={'reviewhour': False}) 
    else:
        description = "Edit Workload Review for TA %s "  % userid
        ta_workloadreviewform = TAWorkloadReviewForm(request.POST or None, instance=taworkload)                
    
    if request.method == "POST":
        if ta_workloadreviewform.is_valid():
            ta_wr = ta_workloadreviewform.save(False)                                    
            ta_wr.save(newsitem_author=Person.objects.get(userid=request.user.username))
           
            l = LogEntry(userid=request.user.username,
                        description=description,
                        related_object=taworkload)
            l.save()

            return HttpResponseRedirect(reverse('offering:view_ta_workload', args=(course.slug, userid)))
        
    context = {'ta':member.person,
               'course':course,               
               'userid':userid,
               'tug': tug,
               'taworkload': taworkload,
               'user_role': 'ADMN',
               'form': ta_workloadreviewform 
               }
    return render(request,'ta/edit_taworkload.html',context)

@_requires_course_staff_or_admin_by_slug
def download_ta_workload(request, course_slug, userid):    
    taworkload = get_object_or_404(TAWorkloadReview, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')
    tug = get_object_or_404(TUG, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')

    response = HttpResponse(content_type="application/pdf")
    filename = "%s_%s_%s_TAWR_%s.pdf" % (taworkload.member.person.last_name, taworkload.member.person.first_name , taworkload.member.person.emplid, course_slug)
    response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
    max_hours = tug.total_hours()
    taworkload_form(taworkload, max_hours, response)
    return response

@_requires_course_staff_or_admin_by_slug
def view_ta_evaluation(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"
    
    taevaluation = get_object_or_404(TAEvaluation, member=member)

    #If the currently logged in user is a TA for the course and is viewing a TUG for another TA, show forbidden message
    # TA can only review their own submitted TA evals on/after first day of next semester
    if curr_user_role=="TA" and (not userid==request.user.username or not taevaluation.is_past_nextsemstart() or taevaluation.draft):
        return ForbiddenResponse(request)
        
    context = {
            'ta':member.person, 
            'course':course,
            'user_role': curr_user_role,
            'eval': taevaluation, 
            }
    return render(request, 'ta/view_taevaluation.html',context)

class TAEvalNewWizard(SessionWizardView):
    form_list = [TAEvaluationForm]
    template_name = 'ta/edit_taevaluation_wizard.html'

    @transaction.atomic
    def dispatch(self, request: HttpRequest, course_slug: str, userid: str) -> HttpResponse:
        if not is_course_instr_by_slug(request, course_slug) and not requires_role(["TAAD", "ADMN"]):
            return ForbiddenResponse(request, "You do not have permission to create TA evaluation")

        return super().dispatch(request, course_slug, userid)
    
    def get_form_initial(self, step):
        init = {}
        course_slug = self.kwargs['course_slug']
        userid = self.kwargs['userid']
        course = get_object_or_404(CourseOffering, slug=course_slug)
        member = get_object_or_404(Member, offering=course, person__userid=userid, role='TA')
        init = {'ta': member, 'course': course, 'draft': True, 'criteria_lab_prep': 5, 'criteria_meet_deadline':5, 'criteria_maintain_hour': 5, 
                'criteria_attend_plan': 5, 'criteria_attend_lec': 5, 'criteria_grading_fair': 5, 'criteria_lab_performance': 5, 
                'criteria_quality_of_feedback': 5, 'criteria_quiz_prep': 5, 'criteria_instr_content': 5, 'criteria_others': 5}
        
        return self.initial_dict.get(step, init)        

    def done(self, form_list, form_dict, **kwargs):
        course_slug = self.kwargs['course_slug']
        userid = self.kwargs['userid']
        course = get_object_or_404(CourseOffering, slug=course_slug)
        member = get_object_or_404(Member, offering=course, person__userid=userid, role='TA')
        
        eval = TAEvaluation()
        for form in form_list:
            for field, value in form.cleaned_data.items():
                setattr(eval, field, value)

        eval.member = member
        if self.request.POST.get("save_draft"):
            eval.draft = True
        else:
            eval.draft = False

        # TA will receive notification if instructors submit it after next term start
        if not eval.draft and eval.is_past_nextsemstart():
            ta_edit_url = reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': member.offering.slug, 'userid': member.person.userid})
            n = NewsItem(user=eval.member.person, source_app="ta_evaluation", title="Your TA Evaluation Form is created. Please review and provide your comment.",
            url=ta_edit_url, author=eval.member.person, content="Your TA Evaluation Form is created. Please review and provide your comment.")
            n.save()

            # Send email notification to each TA (in case they turned off news notication)
            subject = 'Your TA Evaluation for %s is released. Please review and provide your comment.' % course.semester
            plaintext = get_template('ta/emails/notify_ta_eval_release_for_ta.txt')
            url = settings.BASE_ABS_URL + reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': member.offering.slug, 'userid': member.person.userid})
            email_context = {'person': member.person, 'semester': course.semester, 'unit': member.offering.owner, 'url': url}
            from_email = settings.DEFAULT_FROM_EMAIL
            to_email = member.person.email()        
            msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context),
                                from_email=from_email, to=[to_email], headers={'X-coursys-topic': 'ta'})
            msg.send()
            
            eval.config['released'] = True

        eval.save()

        l = LogEntry(userid=self.request.user.username,
                        description= "Create TA Evaluation for TA %s " % userid,
                        related_object=eval)
        l.save()
        
        return HttpResponseRedirect(reverse('offering:view_ta_evaluation',  kwargs=self.kwargs))

class TAEvalEditWizard(SessionWizardView):
    form_list = [TAEvaluationForm]
    template_name = 'ta/edit_taevaluation_wizard.html'

    @transaction.atomic
    def dispatch(self, request: HttpRequest, course_slug: str, userid: str) -> HttpResponse:
        if not is_course_instr_by_slug(request, course_slug) and not requires_role(["TAAD", "ADMN"]):
            return ForbiddenResponse(request, "You do not have permission to edit TA evaluation")

        return super().dispatch(request, course_slug, userid)

    def get_form_initial(self, step):
        init = {}
        course_slug = self.kwargs['course_slug']
        userid = self.kwargs['userid']
        course = get_object_or_404(CourseOffering, slug=course_slug)
        member = get_object_or_404(Member.objects.select_related('person'), offering=course, person__userid=userid, role='TA')
        req = get_object_or_404(TAEvaluation, member__offering__slug=course_slug, member__person__userid=userid, member__role='TA')

        init = {'ta': member, 'course': course, 
                'first_appoint':req.first_appoint,
                'criteria_lab_prep': req.criteria_lab_prep,'criteria_meet_deadline':req.criteria_meet_deadline, 'criteria_maintain_hour': req.criteria_maintain_hour,
                'criteria_attend_plan': req.criteria_attend_plan, 'criteria_attend_lec': req.criteria_attend_lec, 'criteria_grading_fair': req.criteria_grading_fair, 
                'criteria_lab_performance': req.criteria_lab_performance, 'criteria_quality_of_feedback': req.criteria_quality_of_feedback, 
                'criteria_quiz_prep': req.criteria_quiz_prep, 'criteria_instr_content': req.criteria_instr_content, 'criteria_others': req.criteria_others, 
                'criteria_other_comment': req.criteria_other_comment, 'positive_comment': req.positive_comment, 'improve_comment': req.improve_comment,
                'overall_evalation': req.overall_evalation,'recommend_TA': req.recommend_TA, 'no_recommend_comment': req.no_recommend_comment,
                'instructor_sign': req.instructor_sign, 'instructor_signdate': req.instructor_signdate, 'draft': req.draft}
        
        return self.initial_dict.get(step, init)        

    def done(self, form_list, **kwargs):
        course_slug = self.kwargs['course_slug']
        userid = self.kwargs['userid']
        course = get_object_or_404(CourseOffering, slug=course_slug)
        member = get_object_or_404(Member, offering=course, person__userid=userid, role='TA')
        
        eval = get_object_or_404(TAEvaluation, member=member)
        for form in form_list:
            for field, value in form.cleaned_data.items():
                setattr(eval, field, value)
                
        if self.request.POST.get("save_draft"):
            eval.draft = True
        else:
            eval.draft = False

        # TA will receive notification if instructors submit it after next term start
        if not eval.draft and eval.is_past_nextsemstart():
            ta_edit_url = reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': eval.member.offering.slug, 'userid': eval.member.person.userid})
            n = NewsItem(user=eval.member.person, source_app="ta_evaluation", title="Your TA Evaluation Form is created. Please review and provide your comment.",
            url=ta_edit_url, author=None, content="Your TA Evaluation Form is created. Please review and provide your comment.")
            n.save()
            eval.config['released'] = True

            # Send email notification to each TA (in case they turned off news notication)
            subject = 'Your TA Evaluation for %s is released. Please review and provide your comment.' % course.semester
            plaintext = get_template('ta/emails/notify_ta_eval_release_for_ta.txt')
            url = settings.BASE_ABS_URL + reverse('offering:edit_ta_evaluation_by_ta', kwargs={'course_slug': member.offering.slug, 'userid': member.person.userid})
            email_context = {'person': member.person, 'semester': course.semester, 'unit': member.offering.owner, 'url': url}
            from_email = settings.DEFAULT_FROM_EMAIL
            to_email = member.person.email()        
            msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context),
                                from_email=from_email, to=[to_email], headers={'X-coursys-topic': 'ta'})
            msg.send()
            
            eval.config['released'] = True

        eval.save()

        l = LogEntry(userid=self.request.user.username,
                        description= "Edit TA Evaluation for TA %s "  % userid,
                        related_object=eval)
        l.save()

        return HttpResponseRedirect(reverse('offering:view_ta_evaluation',  kwargs=self.kwargs)) 

@_requires_course_staff_or_admin_by_slug
def download_ta_evaluation(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"

    taevaluation = get_object_or_404(TAEvaluation, member=member)

    #If the currently logged in user is a TA for the course and is viewing a TA Eval for another TA, show forbidden message
    # TA can only download their own submitted TA evals on/after first day of next semester    
    if curr_user_role=="TA" and (not userid==request.user.username or not taevaluation.is_past_nextsemstart() or taevaluation.draft):
        return ForbiddenResponse(request)
       
    response = HttpResponse(content_type="application/pdf")    
    filename = "%s_%s_%s_TAEval_%s.pdf" % (member.person.last_name, member.person.first_name , member.person.emplid, course_slug)
    response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
    ta_evaluation_form(taevaluation, member, course, response)
    return response

@_requires_course_staff_or_admin_by_slug
def edit_ta_evaluation_by_ta(request, course_slug, userid):
    course = get_object_or_404(CourseOffering, slug=course_slug)
    member = get_object_or_404(Member, offering=course, person__userid=userid, role="TA")
    try:
        curr_user_role = Member.objects.exclude(role='DROP').get(person__userid=request.user.username, offering=course).role
    except Member.DoesNotExist:
        # we'll just assume this since it's the only other possibility 
        #  since we're checking authorization in the decorator
        curr_user_role = "ADMN"
        
    if not curr_user_role=="TA" and not curr_user_role=="ADMN":
        return ForbiddenResponse(request)
    
    taevaluation = get_object_or_404(TAEvaluation, member=member)
    #If the currently logged in user is a TA for the course and is viewing a TA Eval for another TA, show forbidden message
    # TA can only edit their own submitted TA evals on/after first day of next semester   
    if curr_user_role=="TA" and (not userid==request.user.username or not taevaluation.is_past_nextsemstart() or taevaluation.draft):
        return ForbiddenResponse(request)
    
    # if the TA eval was passed on next semester end, redirect to view page    
    if taevaluation.is_past_nextsemend():
        return HttpResponseRedirect(reverse('offering:view_ta_evaluation', args=(course.slug, userid)))    
        
    taevalformbyTA = TAEvaluationFormbyTA(request.POST or None, instance=taevaluation)
    if request.method == "POST":
        if taevalformbyTA.is_valid():
            taevalformbyTA.save()
                    
            l = LogEntry(userid=request.user.username,
                         description= "Edit TA Evaluation (section E) for TA %s "  % userid,
                         related_object=taevaluation)
            l.save()

            # if TA completed their section (section E)
            if taevaluation.ta_signdate:                        
                _send_notify_to_instr(taevaluation)           
                _send_notify_to_admin(taevaluation, "completed")
                # create news item for TA to view
                ta_view_url = reverse('offering:view_ta_evaluation', kwargs={'course_slug': course.slug, 'userid': userid})
                n = NewsItem(user=taevaluation.member.person, source_app="ta_evaluation", title="Your TA Evaluation Form is completed.",
                url=ta_view_url, author=None, content="Your TA Evaluation Form is completed. You can click here to view")
                n.save()
 
            return HttpResponseRedirect(reverse('offering:view_ta_evaluation', args=(course.slug, userid)))
        
    context = {
            'ta':member.person, 
            'course':course,
            'user_role': curr_user_role,
            'eval': taevaluation, 
            'form': taevalformbyTA
            }
    return render(request, 'ta/edit_taevaluation_by_ta.html',context)


def _send_notify_to_instr(taevaluation):    
    subject = 'TA %s has completed their section of the TA Evaluation for %s' % (taevaluation.member.person.name(), taevaluation.member.offering.semester)
    plaintext = get_template('ta/emails/notify_ta_eval_for_instructor.txt')
    url = settings.BASE_ABS_URL + reverse('offering:view_ta_evaluation', kwargs={'course_slug': taevaluation.member.offering.slug, 'userid': taevaluation.member.person.userid})
    email_context = {'person': taevaluation.member.person, 'offering': taevaluation.member.offering, 'url': url, 'instructor': taevaluation.member.offering.instructors_str()}
    from_email = settings.DEFAULT_FROM_EMAIL          

    instructors = Member.objects.filter(role='INST', offering=taevaluation.member.offering) 
    instructor_email_list = []   
    for member in instructors:
        instructor_email_list.append(member.person.email())

    if len(instructor_email_list) > 0:
        msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context), from_email=from_email,
                                        to=instructor_email_list, headers={'X-coursys-topic': 'ta'})        
        msg.send()

def _send_notify_to_admin(taevaluation, status):    
    subject = 'TA Evaluation for %s for %s' % (taevaluation.member.person.name(), taevaluation.member.offering.semester)     
    plaintext = get_template('ta/emails/notify_'+status+'_ta_eval_for_admin.txt')
    url = settings.BASE_ABS_URL + reverse('offering:view_ta_evaluation', kwargs={'course_slug': taevaluation.member.offering.slug, 'userid': taevaluation.member.person.userid})
    email_context = {'person': taevaluation.member.person, 'offering': taevaluation.member.offering, 'url': url, 'status': status}
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = []

    #/ta
    try:
        posting = TAPosting.objects.get(semester=taevaluation.member.offering.semester, unit=taevaluation.member.offering.owner)
    except:
        posting = None
    if posting: 
        to_email.append(posting.contact().email())                    

    #/tacontracts
    try: 
        hiring_semester = HiringSemester.objects.filter(semester=taevaluation.member.offering.semester, unit=taevaluation.member.offering.owner).first()
    except: 
        hiring_semester = None
    if hiring_semester:
        to_email.append(hiring_semester.contact)

    response = HttpResponse(content_type="application/pdf")   
    ta_evaluation_form(taevaluation, taevaluation.member, taevaluation.member.offering, response)    
    
    if to_email:
        msg = EmailMultiAlternatives(subject=subject, body=plaintext.render(email_context),
                    from_email=from_email, to=to_email, headers={'X-coursys-topic': 'ta'})
        msg.attach(('%s-%s.pdf' % (taevaluation.member.person.emplid, datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))), response.getvalue(),
                        'application/pdf')
        msg.send()

@requires_role("TAAD")
def new_application_manual(request, post_slug):
    get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    return _new_application(request, post_slug, manual=True)

@login_required
def new_application(request, post_slug):
    return _new_application(request, post_slug, manual=False)

@login_required
def edit_application(request, post_slug, userid):
    return _new_application(request, post_slug, manual=False, userid=userid)

@transaction.atomic
def _new_application(request, post_slug, manual=False, userid=None):
    posting = get_object_or_404(TAPosting, slug=post_slug)
    editing = bool(userid)
    is_ta_admin = False
    
    if editing:
        if userid == request.user.username and posting.is_open():
            # can edit own application until closes
            pass
        elif has_role('TAAD', request) and posting.unit in request.units:
            is_ta_admin = True
            pass
        else:
            return ForbiddenResponse(request)

    course_choices = [(c.id, str(c) + " (" + c.title + ")") for c in posting.selectable_courses()]
    course_choices = [(None, '\u2014')] + course_choices
    used_campuses = set((vals['campus'] for vals in posting.selectable_offerings().order_by('campus').values('campus').distinct()))
    skills = Skill.objects.filter(posting=posting)    
    max_courses = posting.max_courses()
    min_courses = posting.min_courses()
    CoursesFormSet = formset_factory(CoursePreferenceForm, min_num=max_courses, max_num=max_courses)

    sin = None
    # build basic objects, whether new or editing application
    if editing:
        person = Person.objects.get(userid=userid)
        application = get_object_or_404(TAApplication, posting=posting, person__userid=userid)
        old_coursepref = CoursePreference.objects.filter(app=application).exclude(rank=0).order_by('rank')
    else:
        application = None
    
    if not manual:
        """
        Don't change the person in the case of a TA Admin editing, as we don't want to save this application as the
        Admin's, but as the original user's.
        """
        if not is_ta_admin:
            try:
                person = ensure_person_from_userid(request.user.username)
            except SIMSProblem:
                return HttpError(request, status=503, title="Service Unavailable", error="Currently unable to handle the request.", errormsg="Problem with SIMS connection while trying to find your account info")

        if not person:
            return NotFoundResponse(request, "Unable to find your computing account in the system: this is likely because your account was recently activated, and it should be fixed tomorrow. If not, email %s." % (help_email(request),))

        existing_app = TAApplication.objects.filter(person=person, posting=posting)
        if not userid and existing_app.count() > 0: 
            messages.success(request, "You have already applied for the %s %s posting." % (posting.unit, posting.semester))
            return HttpResponseRedirect(reverse('ta:view_application', kwargs={'post_slug': existing_app[0].posting.slug, 'userid': existing_app[0].person.userid}))

        if person.sin() != person.defaults['sin']:
            sin = person.sin()
       
    if request.method == "POST":
        search_form = StudentSearchForm(request.POST)
        #Try to manually retrieve person
        if manual:
            try:
                person = get_object_or_404(Person, emplid=int(request.POST['search']))
            except ValueError:
                search_form = StudentSearchForm(request.POST['search'])
                messages.error(request, "Invalid emplid %s for person." % (request.POST['search']))
                return HttpResponseRedirect(reverse('ta:new_application_manual', args=(post_slug,)))
            
            #Check to see if an application already exists for the person 
            existing_app = TAApplication.objects.filter(person=person, posting=posting)
            if existing_app.count() > 0: 
                messages.success(request, "%s has already applied for the %s %s posting." % (person, posting.unit, posting.semester))
                return HttpResponseRedirect(reverse('ta:view_application', kwargs={'post_slug': existing_app[0].posting.slug, 'userid': existing_app[0].person.userid}))
        
        if editing:
            ta_form = TAApplicationForm(request.POST, request.FILES, prefix='ta', initial={'unit': posting.unit.id}, instance=application)
        else:
            ta_form = TAApplicationForm(request.POST, request.FILES, prefix='ta', initial={'unit': posting.unit.id})

        ta_form.add_extra_questions(posting)

        courses_formset = CoursesFormSet(request.POST)
        for f in courses_formset:
            f.fields['course'].choices = course_choices

        if ta_form.is_valid() and courses_formset.is_valid():
            # No duplicates allowed
            courses = []
            for (rank,form) in enumerate(courses_formset):
                if 'course' in form.cleaned_data and form.cleaned_data['course']:
                    courses.append( form.cleaned_data['course'] )

            if len(courses) != len(set(courses)):
                messages.error(request, "You have selected duplicate courses. Please select 5 different courses. ")
            else:
                app = ta_form.save(commit=False)
                if 'extra_questions' in posting.config and len(posting.config['extra_questions']) > 0:
                    temp = {}
                    for question in posting.config['extra_questions']:
                        temp[question] = ta_form.cleaned_data[question] 
                    app.config['extra_questions'] = temp

                # if they gave a SIN, populate the Person record
                if app.sin and app.sin != ta_form.sin_default:
                    if person.sin() != app.sin:
                        person.set_sin(app.sin)
                        person.save()
                
                today = datetime.date.today()
                if(posting.closes < today):
                    app.late = True
                else:
                    app.late = False
                app.posting = posting
                app.person = person
                if manual:
                    app.admin_create = True

                # Add our attachments (resume and transcript if included.)
                if request.FILES and 'ta-resume' in request.FILES:
                    resume = request.FILES['ta-resume']
                    resume_file_type = resume.content_type
                    if resume.charset:
                        resume_file_type += "; charset=" + resume.charset
                    app.resume = resume
                    app.resume_mediatype = resume_file_type

                if request.FILES and 'ta-transcript' in request.FILES:
                    transcript = request.FILES['ta-transcript']
                    transcript_file_type = transcript.content_type
                    if transcript.charset:
                        transcript_file_type += "; charset=" + transcript.charset
                    app.transcript = transcript
                    app.transcript_mediatype = transcript_file_type


                app.save()
                ta_form.save_m2m()
                
                # extract campus and skill values; create objects
                CampusPreference.objects.filter(app=app).delete()
                for c in used_campuses:
                    val = request.POST.get('campus-'+c, None)
                    if val not in PREFERENCES:
                        val = 'NOP'
                    cp = CampusPreference(app=app, campus=c, pref=val)
                    cp.save()
                
                SkillLevel.objects.filter(app=app).delete()
                for s in skills:
                    val = request.POST.get('skill-'+str(s.position), None)
                    if val not in LEVELS:
                        val = 'NONE'
                    sl = SkillLevel(skill=s, app=app, level=val)
                    sl.save()
                
                # save course preferences: update existing or create new, as needed
                old_pref = set(CoursePreference.objects.filter(app=app))
                used_pref = set()
                for (rank,form) in enumerate(courses_formset):
                    existing_crs = CoursePreference.objects.filter(app=app, course=form.cleaned_data['course'])
                    if existing_crs:
                        course = existing_crs[0]
                        #course.exper = form.cleaned_data['exper']
                        #course.taken = form.cleaned_data['taken']
                    else:
                        course = form.save(commit=False)
                    course.app = app
                    course.rank = rank+1
                    if course.course_id:
                        course.save()
                        used_pref.add(course)
                
                # any removed courses: set to rank=0, but don't delete (since we assume one exists if it has been assigned already)
                for course in old_pref - used_pref:
                    course.rank = 0
                    course.save()
                
                # Email confirmation to student if everything is saved
                # Save the event to log file
                if not manual and not editing:
                    app.email_application()                    

                l = LogEntry(userid=request.user.username,
                        description="Submitted TA position for %s %s (%s in %s)." % (app.person.name(), app.person.userid, app.posting.semester, app.posting.unit),
                        related_object=app)
                l.save()
                
                return HttpResponseRedirect(reverse('ta:view_application', kwargs={'post_slug': app.posting.slug, 'userid': app.person.userid}))
        
        # redisplaying form: build values for template with entered values
        campus_preferences = []
        for c in used_campuses:
            val = request.POST.get('campus-'+c, None)
            if val not in PREFERENCES:
                val = 'NOP'
            campus_preferences.append((c, CAMPUSES[c], val))
        skill_values = []
        for s in skills:
            val = request.POST.get('skill-'+str(s.position), None)
            if val not in LEVELS:
                val = 'NONE'
            skill_values.append((s.position, s.name, val))

    elif editing:
        # editing: build initial form from existing values
        
        ta_form = TAApplicationForm(prefix='ta', initial={'unit': posting.unit.id}, instance=application)
        # Stupidly, the filefields don't consider themselves "filled" if we have a previous instance that contained
        # the right fields anyway.  Manually check and clear the required part.
        if application.resume:
            ta_form.fields['resume'].required = False
        if application.transcript:
            ta_form.fields['transcript'].required = False
        ta_form.add_extra_questions(posting)
        cp_init = [{'course': cp.course, 'taken': cp.taken, 'exper':cp.exper} for cp in old_coursepref]
        search_form = None
        courses_formset = CoursesFormSet(initial=cp_init)
        for f in courses_formset:
            f.fields['course'].choices = course_choices

        # build values for template with entered values
        campus_preferences = []
        for c in used_campuses:
            try:
                val = CampusPreference.objects.get(app=application, campus=c).pref
            except CampusPreference.DoesNotExist:
                val = 'NOP'
            if val not in PREFERENCES:
                val = 'NOP'
            campus_preferences.append((c, CAMPUSES[c], val))
        skill_values = []
        for s in skills:
            try:
                val = SkillLevel.objects.get(app=application, skill=s).level
            except SkillLevel.DoesNotExist:
                val = 'WIL'
            if val not in LEVELS:
                val = 'NONE'
            skill_values.append((s.position, s.name, val))
    else:
        # new application
        search_form = StudentSearchForm()
        courses_formset = CoursesFormSet()
        for f in courses_formset:
            f.fields['course'].choices = course_choices
        ta_form = TAApplicationForm(prefix='ta', initial={'sin': sin, 'unit': posting.unit.id})
        ta_form.add_extra_questions(posting)
        campus_preferences = [(lbl, name, 'WIL') for lbl,name in CAMPUS_CHOICES if lbl in used_campuses]
        skill_values = [(s.position, s.name, 'NONE') for s in skills]
        today = datetime.date.today()
        if(posting.closes < today):
            messages.warning(request, "The closing date for this posting has passed.  Your application will be marked 'late' and may not be considered.")

    context = {
                    'posting':posting,
                    'manual':manual,
                    'editing': editing,
                    'ta_form':ta_form,
                    'search_form':search_form,
                    'courses_formset':courses_formset,
                    'campus_preferences':campus_preferences,
                    'campus_pref_choices':PREFERENCE_CHOICES,
                    'skill_values': skill_values,
                    'skill_choices': LEVEL_CHOICES,
                    'instructions': posting.instructions(),
                    'hide_campuses': posting.hide_campuses(),
                    'send_notify': posting.send_notify()
                  }
    return render(request, 'ta/new_application.html', context)


@login_required
def get_info(request, post_slug):
    """
    AJAX callback for SIMS data (displayed so applicant can see problems)
    """
    p = get_object_or_404(Person, userid=request.user.username)
    try:
        data = more_personal_info(emplid=p.emplid, needed=['phones'])
    except SIMSProblem as e:
        data = {'error': str(e)}
    return HttpResponse(json.dumps(data), content_type='application/json')

@requires_role("TAAD")
def update_application(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid, posting__unit__in=request.units)
    application.late = False
    application.save()
    messages.success(request, "Removed late status from the application.")
    return HttpResponseRedirect(reverse('ta:view_application', kwargs={'post_slug': application.posting.slug, 'userid': application.person.userid}))
    
@requires_role("TAAD")
def view_all_applications(request,post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    applications = TAApplication.objects.filter(posting=posting)
    context = {
            'applications': applications,
            'posting': posting,
            }
    return render(request, 'ta/view_all_applications.html', context)

@requires_role("TAAD")
def download_all_applications(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    applications = TAApplication.objects.filter(posting=posting)
    response = HttpResponse(content_type='text/csv')

    response['Content-Disposition'] = 'inline; filename="ta_applications-%s-%s.csv"' % \
                                      (posting.semester.name, datetime.datetime.now().strftime('%Y%m%d'))
    writer = csv.writer(response)
    if applications:
        writer.writerow(['Person', 'Grad Program', 'ID', 'Email', 'Category', 'Current Program', 'Other program comment', 'Supervisor', 'Max BUs', 'Ranked', 'Campus Preferences', 'Skill',  'Assigned', 'Assigned BUs' ])

        for a in applications:
            writer.writerow([a.person.sortname(), a.grad_program_information(), a.person.emplid, a.person.email(), a.get_category_display(), a.get_current_program_display(), a.program_comment, a.supervisor,
                             a.base_units, a.course_pref_display(), a.campus_pref_display(), a.skill_level_display(), a.course_assigned_display(), a.base_units_assigned()])
    return response


@requires_role("TAAD")
def print_all_applications(request,post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    applications = TAApplication.objects.filter(posting=posting).order_by('person')

    for application in applications:
        application.courses = CoursePreference.objects.filter(app=application).exclude(rank=0).order_by('rank')
        application.skills = SkillLevel.objects.filter(app=application).select_related('skill')
        application.campuses = CampusPreference.objects.filter(app=application)
        application.contracts = TAContract.objects.filter(application=application)
        application.previous_experience = TACourse.objects.filter(contract__application__person=application.person) \
            .exclude(contract__application=application).select_related('course__semester')
        application.grad_programs = GradStudent.objects \
             .filter(program__unit__in=request.units, person=application.person)

    context = {
            'applications': applications,
            'posting': posting,
            }
    return render(request, 'ta/print_all_applications.html', context)

@requires_role("TAAD")
def print_all_applications_by_course(request,post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).select_related('course')
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]
    
    # collect all course preferences in a sensible way
    prefs = CoursePreference.objects.filter(app__posting=posting).exclude(rank=0).order_by('app__person').select_related('app', 'course')
    
    for offering in offerings: 
        offering.applications = []
        applications_for_this_offering = [pref.app for pref in prefs if 
            (pref.course.number == offering.course.number and pref.course.subject == offering.course.subject)]
        for application in applications_for_this_offering:
            if not hasattr(application, 'extra_data_done'):
                application.courses = CoursePreference.objects.filter(app=application).exclude(rank=0).order_by('rank')
                application.skills = SkillLevel.objects.filter(app=application).select_related('skill')
                application.campuses = CampusPreference.objects.filter(app=application)
                application.contracts = TAContract.objects.filter(application=application)
                application.previous_experience = TACourse.objects.filter(contract__application__person=application.person) \
                    .exclude(contract__application=application).select_related('course__semester')
                application.grad_programs = GradStudent.objects \
                     .filter(program__unit__in=request.units, person=application.person)
                application.extra_data_done = True

            offering.applications.append(application)

    context = {
            'offerings': offerings,
            'posting': posting,
            }
    return render(request, 'ta/print_all_applications_by_course.html', context)


@login_required
def view_application(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid)
    is_ta_admin = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username,
                                      unit=application.posting.unit).count() > 0

    # Only TA Administrator or owner of application can view it
    if application.person.userid != request.user.username and not is_ta_admin:
        return ForbiddenResponse(request, 'You cannot access this application')

    units = [application.posting.unit]
   
    application.courses = CoursePreference.objects.filter(app=application).exclude(rank=0).order_by('rank')
    application.skills = SkillLevel.objects.filter(app=application).select_related('skill')
    application.campuses = CampusPreference.objects.filter(app=application)
    application.contracts = TAContract.objects.filter(application=application)
    application.previous_experience = TACourse.objects.filter(contract__application__person=application.person) \
            .exclude(contract__application=application).select_related('course__semester')
    application.grad_programs = GradStudent.objects \
                 .filter(program__unit__in=units, person=application.person)

    if is_ta_admin and application.courses:
        contract = application.courses[0]
    else:
        contract = None

    context = {
            'application':application,
            'contract': contract,
            'is_ta_admin': is_ta_admin,
            }
    return render(request, 'ta/view_application.html', context)


@requires_role("TAAD")
def view_resume(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid)
    is_ta_admin = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username,
                                      unit=application.posting.unit).count() > 0
    if not is_ta_admin:
        return ForbiddenResponse(request, 'You cannot access this application')
    resume = application.resume
    filename = resume.name.rsplit('/')[-1]
    resp = StreamingHttpResponse(resume.chunks(), content_type=application.resume_mediatype)
    resp['Content-Disposition'] = 'inline; filename="' + filename + '"'
    resp['Content-Length'] = resume.size
    return resp


@requires_role("TAAD")
def download_resume(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid)
    is_ta_admin = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username,
                                      unit=application.posting.unit).count() > 0
    if not is_ta_admin:
        return ForbiddenResponse(request, 'You cannot access this application')
    resume = application.resume
    #filename = application.person.name() + '-' + resume.name.rsplit('/')[-1]
    original_filename = resume.name.rsplit('/')[-1]
    filename = "%s_%s_%s_CV.%s" % (application.person.last_name, application.person.first_name , 
                                                                              application.person.emplid, original_filename.rsplit('.')[-1])
    resp = StreamingHttpResponse(resume.chunks(), content_type=application.resume_mediatype)
    resp['Content-Disposition'] = 'attachment; filename="' + filename + '"'
    resp['Content-Length'] = resume.size
    return resp


@requires_role("TAAD")
def view_transcript(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid)
    is_ta_admin = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username,
                                      unit=application.posting.unit).count() > 0
    if not is_ta_admin:
        return ForbiddenResponse(request, 'You cannot access this application')
    transcript = application.transcript
    filename = transcript.name.rsplit('/')[-1]
    resp = StreamingHttpResponse(transcript.chunks(), content_type=application.transcript_mediatype)
    resp['Content-Disposition'] = 'inline; filename="' + filename + '"'
    resp['Content-Length'] = transcript.size
    return resp


@requires_role("TAAD")
def download_transcript(request, post_slug, userid):
    application = get_object_or_404(TAApplication, posting__slug=post_slug, person__userid=userid)
    is_ta_admin = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username,
                                      unit=application.posting.unit).count() > 0
    if not is_ta_admin:
        return ForbiddenResponse(request, 'You cannot access this application')
    transcript = application.transcript
    #filename = application.person.name() + '-' + transcript.name.rsplit('/')[-1]
    original_filename = transcript.name.rsplit('/')[-1]
    filename = "%s_%s_%s_transcript.%s" % (application.person.last_name, application.person.first_name , 
                                                                              application.person.emplid, original_filename.rsplit('.')[-1])
    resp = StreamingHttpResponse(transcript.chunks(), content_type=application.transcript_mediatype)
    resp['Content-Disposition'] = 'attachment; filename="' + filename + '"'
    resp['Content-Length'] = transcript.size
    return resp



@requires_role("TAAD")
def view_late_applications(request,post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    applications = TAApplication.objects.filter(posting__slug=post_slug, late=True)
    context = {
        'posting': posting,
        'applications':applications,
    }
    return render(request, 'ta/late_applications.html', context)

@login_required
def view_postings(request):
    roles = Role.objects_fresh.filter(role="TAAD", person__userid=request.user.username)
    units = [r.unit for r in roles]
    today = datetime.date.today()
    postings = TAPosting.objects.filter(opens__lte=today, closes__gte=today).order_by('-semester', 'unit')
    owned = TAPosting.objects.filter(unit__in=units).order_by('-semester', 'unit')
    context = {
            'postings': postings,
            'owned': owned,
            'is_admin': bool(roles),
            }
    return render(request, 'ta/view_postings.html', context) 

@requires_role("TAAD")
def assign_tas(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')
    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).select_related('course')

    # decorate offerings with currently-assigned TAs
    all_assignments = TACourse.objects.filter(contract__posting=posting).select_related('course', 'contract__application__person', 'course__semester')
    for o in all_offerings:
        o.assigned = [crs for crs in all_assignments if crs.course_id == o.id and crs.contract.bu() > 0]
    
    # ignore excluded courses
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]
    excluded = [o for o in all_offerings if o.course_id in excl]
    
    context = {'posting': posting, 'offerings': offerings, 'excluded': excluded}
    return render(request, 'ta/assign_tas.html', context)


@requires_role("TAAD")
def download_assign_csv(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')

    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).exclude(component='CAN')

    # decorate offerings with currently-assigned TAs
    all_assignments = TACourse.objects.filter(contract__posting=posting).select_related('course',
                                                                                        'contract__application__person')
    for o in all_offerings:
        o.assigned = [crs for crs in all_assignments if crs.course == o and crs.contract.bu() > 0]        

    # ignore excluded courses
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s-assigsnment-table.csv"' % (posting.slug)
    writer = csv.writer(response)
    writer.writerow(['Offering', 'Combined to (from SIMS)', 'Instructor', 'Enrollment', 'Campus', 'Assigned', 'Applicants', 'Required BU (by capacity)',
                    'Required BU (by enrol)', 'Assigned Total BU', 'Remaining'])
    for o in offerings:
        enrollment_string = '%s/%s' % (o.enrl_tot, o.enrl_cap)
        if o.wait_tot:
            enrollment_string += ' (+%s)' % o.wait_tot

        assigned_strings = []
        for tacrs in o.assigned:
            if tacrs.total_bu > 0:
                assigned_strings.append(tacrs.contract.application.person.sortname() + ' (' + str(tacrs.total_bu) + ')')
        assigned_string = ', '.join(assigned_strings)
        required_bus = str(posting.required_bu(o, count=o.enrl_tot))
        if o.extra_bu() != 0:
            required_bus += '(%s +%s)' % (posting.default_bu(o, count=o.enrl_cap), o.extra_bu_str())

        joint_with = o.config.get('joint_with')
        newlist = ''   

        if joint_with:
                for index, j in enumerate(joint_with):
                    start = j.find('-')+1
                    if j[-2:] == '00':
                        joint_course = j.upper()[start:].replace("-", " ")[:-2]
                    else:
                        joint_course = j.upper()[start:].replace("-", " ")
                    
                    if joint_course > o.name():
                        joint_with = o.name() + "/" + joint_course
                    else:
                        joint_with = joint_course + "/" + o.name()
                    if index == 0:
                        newlist = joint_with
                    else:
                        newlist += ', '+ joint_with

        writer.writerow([o.name(), newlist, o.instructors_str(), enrollment_string, o.get_campus_display(), assigned_string,
                         posting.applicant_count(o), posting.required_bu(o, count=o.enrl_cap), required_bus, posting.assigned_bu(o),
                         posting.required_bu(o)-posting.assigned_bu(o)])
    return response


@requires_role("TAAD")
@transaction.atomic
def assign_bus(request, post_slug, course_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    offering = get_object_or_404(CourseOffering, slug=course_slug)
    instructors = offering.instructors()
    course_prefs = CoursePreference.objects.filter(app__posting=posting, course=offering.course, app__late=False).select_related('app')
    tacourses = TACourse.objects.filter(course=offering).select_related('contract__application', 'contract__application__person')
    tacourses = list(tacourses)
    all_applicants = TAApplication.objects.filter(posting=posting).select_related('person', 'supervisor').prefetch_related('campuspreference_set')
    descrs = CourseDescription.objects.filter(unit=posting.unit)
    if not descrs.filter(labtut=True) or not descrs.filter(labtut=False):
        messages.error(request, "Must have at least one course description for TAs with and without labs/tutorials before assigning TAs.")
        return HttpResponseRedirect(reverse('ta:descriptions', kwargs={}))
    
    if request.method == "POST" and 'extra_bu' in request.POST:
        # update extra BU and lab/tutorial setting for course
        response_data = {
            'message': "",
            'error': False,
            'labtas_changed':False,
            'has_labtas':  offering.labtas() }
        
        extra_bu = request.POST['extra_bu']
        if extra_bu != '':
            try:
                extra_bu = decimal.Decimal(extra_bu)
                if extra_bu != offering.extra_bu():
                    offering.config['extra_bu'] = extra_bu
                    response_data['message'] = "Extra Base Units set to " + str(extra_bu)
            except:
                response_data['message'] = "Extra Base Units must be a decimal number"
                response_data['error'] = True

        if 'labtas' in request.POST and request.POST['labtas']=="true":
            if offering.labtas() != True:
                offering.config['labtas'] = True
                response_data['labtas_changed'] = True
                response_data['has_labtas'] = True
        else:
            if offering.labtas() != False:
                offering.config['labtas'] = False
                response_data['labtas_changed'] = True
                response_data['has_labtas'] = False

        if response_data['message'] == '':
            response_data['message'] = "Success!"

        offering.save()
        
        # if the course's Lab (+0.17) status has changed, change all TA contracts to conform to the new Lab (+0.17) status.
        if response_data['labtas_changed']:
            for ta_course in TACourse.objects.filter( course=offering ).select_related('contract__posting'):
                if ta_course.contract.posting == posting and ta_course.description != ta_course.default_description():
                    # change the description
                    ta_course.description = ta_course.default_description()
                    ta_course.save()
                    # this requires that the contract be re-signed
                    ta_course.contract.status = 'NEW'
                    ta_course.contract.save()

        return HttpResponse(json.dumps(response_data), content_type='application/json')

    applicants = []
    assigned_ta = []
    initial = [] # used to initialize formset
    personids = []

    # First, people who have assigned BUs
    for ta_course in tacourses:
        applicant = ta_course.contract.application
        applicant.course_rank = 0
        applicants.append(applicant)
    
    # Then, people who have this course in their preferences.
    for course_preference in course_prefs:
        applicant = course_preference.app
        
        # Determine Rank
        applicant.course_rank = course_preference.rank
        personids.append(applicant.person_id)

        if applicant not in applicants:
            applicants.append(applicant)
        else:
            for existing_applicant in applicants:
                if existing_applicant == applicant:
                    existing_applicant.course_rank = course_preference.rank

    # collect all grad program who apply ta posting
    active_gs = GradStudent.objects.filter(person_id__in=personids, current_status__in=STATUS_REAL_PROGRAM) \
                .select_related('program__unit').order_by("person_id","-created_at")    

    # collect all Campus Preference
    campus_preference = CampusPreference.objects.filter(campus=offering.campus).select_related('app')
       
    # Then, anybody else. 
    for applicant in all_applicants:
        applicant.course_rank = 99
        if applicant not in applicants:
            applicants.append(applicant)

    for applicant in applicants:
        # Determine Current Grad Status
        for g in active_gs:
                if g.person_id == applicant.person_id:
                    applicant.active_gs = g
                    break
                    
        #applicant.active_gs = GradStudent.objects.filter(person=applicant.person, current_status__in=STATUS_REAL_PROGRAM) \
        #        .select_related('program__unit').order_by("-created_at").first()
        
        # Determine  Campus Preference
        for c in campus_preference:
                if c.app == applicant:
                    applicant.campus_preference = c
                    break
                
        # Determine Campus Preference
        #try:
        #    campus_preference = CampusPreference.objects.get(app=applicant, campus=offering.campus)
        #except CampusPreference.DoesNotExist:
            # temporary fake object: shouldn't happen, but don't die if it does.
        #    campus_preference = CampusPreference(app=applicant, campus=offering.campus, pref="NOT")
        #applicant.campus_preference = campus_preference

        #Find BU assigned to this applicant through contract
        course_assignments = [c for c in tacourses if c.contract.application_id == applicant.id]
        if len(course_assignments) == 1:
            assignment_for_this_course = course_assignments[0]
            applicant.assigned_course = assignment_for_this_course
        else:
            applicant.assigned_course = None

        # Set initial values for Formset
        init = {}
        if applicant.assigned_course:
            assignment_for_this_course = course_assignments[0]
            init['bu'] = assignment_for_this_course.bu
        init['rank'] = applicant.rank
        initial.append(init)


    AssignBUFormSet = formset_factory(AssignBUForm)
    
    #Save ranks and BU's
    if request.method == "POST":
        formset = AssignBUFormSet(request.POST)
        if formset.is_valid():
            descr_error = False
            for i in range(len(applicants)):
                #update rank
                applicant = applicants[i]
                applicant.rank = formset[i]['rank'].value()
                applicant.save()
                
                if not applicant.assigned_course: 
                    #create new TACourse if bu field is nonempty
                    if formset[i]['bu'].value() != '' and formset[i]['bu'].value() != '0':
                        #create new TAContract if there isn't one
                        contracts = list(TAContract.objects.filter(application=applicants[i], posting=posting))
                        if len(contracts) > 0: #count is 1
                            # if we've added to the contract, we've invalidated it. 
                            contract = contracts[0]
                            contract.status = "NEW"
                        else:
                            contract = TAContract(created_by=request.user.username)
                            contract.first_assign(applicants[i], posting)
                        bu = formset[i]['bu'].value()
                        tacourse = TACourse(course=offering, contract=contract, bu=bu)
                        try:
                            tacourse.description = tacourse.default_description()
                        except ValueError:
                            # handle the case where no appropriate default CourseDescription object can be found
                            descr_error = True
                            formset[i]._errors['bu'] = formset[i].error_class(["Can't find a contract description to assign to the contract."])
                        else:
                            tacourse.save()
                            contract.save()
                else: 
                    #update bu for existing TACourse
                    if formset[i]['bu'].value() != '' and formset[i]['bu'].value() != '0':
                        old_bu = decimal.Decimal(formset[i]['bu'].value())
                        new_bu = applicant.assigned_course.bu
                        if old_bu != new_bu:
                            applicant.assigned_course.bu = formset[i]['bu'].value()
                            applicant.assigned_course.save()                        
                            # if we've changed the contract, we've invalidated it. 
                            contract = applicant.assigned_course.contract
                            contract.status = "NEW"
                            contract.save()
                    #unassign bu to this offering for this applicant
                    else:
                        course = applicant.assigned_course
                        course.delete()
            if not descr_error:
                return HttpResponseRedirect(reverse('ta:assign_tas', args=(post_slug,)))
    else:
        formset = AssignBUFormSet(initial=initial)
    
    context = {'formset':formset, 'posting':posting, 'offering':offering, 'instructors':instructors,
               'applications': applicants, 'LAB_BONUS': LAB_BONUS}
    return render(request, 'ta/assign_bu.html', context) 

@requires_role("TAAD")
def all_contracts(request, post_slug):
    #name, appointment category, rank, deadline, status. Total BU, Courses TA-ing , view/edit
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    contracts = TAContract.objects.filter(posting=posting).order_by('application__person')
    paginator = Paginator(contracts,5)

    descrs = CourseDescription.objects.filter(unit=posting.unit)
    if not descrs.filter(labtut=True) or not descrs.filter(labtut=False):
        messages.error(request, "Must have at least one course description for TAs with and without labs/tutorials before assigning TAs.")
        return HttpResponseRedirect(reverse('ta:descriptions', kwargs={}))

    existing = set(c.application_id for c in TAContract.objects.filter(posting=posting).select_related('application'))
    queryset = TAApplication.objects.filter(posting=posting).exclude(id__in=existing).order_by('person')
    application_choices = [(a.id, a.person.name()) for a in queryset if a not in existing]
    form = NewTAContractForm()
    form.fields['application'].choices = application_choices
    
    try:
        p = int(request.GET.get("page",'1'))
    except ValueError:p=1
    
    try:
        contract_page = paginator.page(p)
    except(InvalidPage, EmptyPage):
        contract_page.paginator.page(paginator.num_pages)
    
    if request.method == "POST" and 'sendoffers' in request.POST:
        # POST request to send offers
        ccount = 0
        from_user = posting.contact()
        for contract in contracts:
            cname = 'contract_%s' % contract.id
            if cname in request.POST:
                app = contract.application.person
                offer_url = reverse('ta:accept_contract', kwargs={'post_slug': post_slug, 'userid': app.userid})
                contract.status = 'OPN'
                _create_news(app, offer_url, from_user, contract.deadline, contract.total_bu())
                contract.save()
                ccount += 1
                
        if ccount > 1:
            messages.success(request, "Successfully sent %s offers." % ccount)
        elif ccount > 0:
            messages.success(request, "Successfully sent offer.")

    elif request.method == "POST":
        # POST request to set a contract to "signed"
        for key in request.POST:
            if key.startswith('signed-'):
                userid = key[7:]
                contracts = TAContract.objects.filter(posting=posting, application__person__userid=userid)
                if contracts:
                    contract = contracts[0]
                    contract.status = 'SGN'
                    contract.save()
                    messages.success(request, "Contract for %s signed." % (contract.application.person.name()))
                    l = LogEntry(userid=request.user.username,
                        description="Set contract for %s to signed." % (contract.application.person.name()),
                        related_object=contract)
                    l.save()
                return HttpResponseRedirect(reverse('ta:all_contracts', kwargs={'post_slug': posting.slug}))

    
    # Create a list of courses that this TA is assigned to. 
    for contract in contracts:
        crs_list = ''
        courses = TACourse.objects.filter(contract=contract)
        for course in courses:
            crs_list += course.course.subject+" "+course.course.number+" "+course.course.section+" ("+str(course.total_bu)+")\n"
        contract.crs_list = crs_list
        if contract.status == 'ACC' and contract.config.get('accepted_date') is not None:            
            contract.accrej_date = iso8601.parse_date(contract.config.get('accepted_date'))
        if contract.status == 'REJ' and contract.config.get('rejected_date') is not None:
            contract.accrej_date = iso8601.parse_date(contract.config.get('rejected_date'))    
            
    #postings = TAPosting.objects.filter(unit__in=request.units).exclude(Q(semester=posting.semester))
    applications = TAApplication.objects.filter(posting=posting).exclude(Q(id__in=TAContract.objects.filter(posting=posting).values_list('application', flat=True)))
    return render(request, 'ta/all_contracts.html',
                  {'contracts':contracts, 'posting':posting, 'applications':applications, 'form': form})


@requires_role("TAAD")
def contracts_table_csv(request, post_slug):
    # The contracts_csv view is actually a payroll upload file, with way more fields.  This one is basically
    # the exact same as all_contracts, but in CSV format.
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    contracts = TAContract.objects.filter(posting=posting)
    for contract in contracts:
        crs_list = ''
        courses = TACourse.objects.filter(contract=contract)
        for course in courses:
            crs_list += course.course.subject+" "+course.course.number+" "+course.course.section+" ("+str(course.total_bu)+")\n"
        contract.crs_list = crs_list
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s-table.csv"' % (posting.slug)
    writer = csv.writer(response)
    writer.writerow(['Person', 'Email', 'Citizenship', 'Appt Category', 'Rank', 'Status', 'Acc/Rej Date', 'Total BU', 'TA Courses', 'Deadline'])
    for c in contracts:
        citizen = ''
        if c.application.person.citizen():
            citizen = str(c.application.person.citizen())
        else:
            citizen = "unknown"
        if c.application.person.visa():
            citizen += "(visa: "+ str(c.application.person.visa()) + ")"
        else:
            citizen += "(visa: unknown)"

        statusdate = ''
        if c.status == 'ACC' and c.config.get('accepted_date') is not None:            
            statusdate = iso8601.parse_date(c.config.get('accepted_date')).strftime("%Y/%m/%d")
        if c.status == 'REJ' and c.config.get('rejected_date') is not None:
            statusdate = iso8601.parse_date(c.config.get('rejected_date')).strftime("%Y/%m/%d")

        writer.writerow([c.application.person, c.application.person.email(), citizen, c.get_appt_category_display() + '(' + c.appt_category + ')',
                         c.application.rank, c.get_status_display(), statusdate, c.total_bu(), c.crs_list, c.deadline])
    return response


@requires_role("TAAD")
def contracts_csv(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s.csv"' % (posting.slug)
    writer = csv.writer(response)
    writer.writerow(['Batch ID', 'Term ID', 'Contract Signed', 'Benefits Indicator', 'EmplID', 'SIN',
                     'Last Name', 'First Name 1', 'First Name 2', 'Payroll Start Date', 'Payroll End Date',
                     'Action', 'Action Reason', 'Position Number', 'Job Code', 'Full_Part time', 'Pay Group',
                     'Employee Class', 'Category', 'Project', 'Object', 'Fund', 'Dept ID (cost center)', 'Program',
                     'Prep Units', 'Base Units', 'Appt Comp Freq', 'Semester Base Salary Rate',
                     'Biweekly Base Salary Pay Rate', 'Hourly Rate', 'Standard Hours', 'Scholarship Rate Code',
                     'Semester Scholarship Salary Pay Rate', 'Biweekly Scholarship Salary Pay Rate', 'Lump Sum Amount',
                     'Lump Sum Hours', 'Scholarship Lump Sum'])
    
    contracts = TAContract.objects.filter(posting=posting, status__in=['ACC', 'SGN']) \
                .select_related('application__person')
    seq = posting.next_export_seq()
    batchid = '%s_%s_%02i' % (posting.unit.label, datetime.date.today().strftime("%Y%m%d"), seq)
    for c in contracts:
        bu = c.bu()
        total_bu = c.total_bu()
        prep_units = c.prep_bu()
        
        signed = 'Y' if c.status=='SGN' else 'N'
        benefits = 'Y'
        schol_rate = 'TSCH' if c.scholarship_per_bu else ''
        salary_total = total_bu * c.pay_per_bu
        schol_total = bu * c.scholarship_per_bu
        if prep_units == 0:
            prep_units = ''
        
        row = [batchid, posting.semester.name, signed, benefits, c.application.person.emplid, c.application.sin]
        row.extend([c.application.person.last_name, c.application.person.first_name, c.application.person.middle_name])
        row.extend([c.pay_start.strftime("%Y%m%d"), c.pay_end.strftime("%Y%m%d"), 'REH', 'REH'])
        row.extend(["%08i" % c.position_number.position_number, '', '', 'TSU', '', c.application.category])
        row.extend(['', c.position_number.account_number, 11, posting.unit.deptid(),  90150, prep_units, bu])
        row.extend(['T', "%.2f" % salary_total, '', '', '', schol_rate, "%.2f" % schol_total, '', '', '', ''])
        writer.writerow(row)
    
    return response
    

@requires_role("TAAD")
def preview_offer(request, post_slug, userid):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    contract = get_object_or_404(TAContract, posting=posting, application__person__userid=userid)
    return accept_contract(request, posting.slug, userid, preview=True)
    
    
@login_required
def accept_contract(request, post_slug, userid, preview=False):
    if not preview and request.user.username != userid:
        return ForbiddenResponse(request, 'You cannot access this page')

    posting = get_object_or_404(TAPosting, slug=post_slug)
    person = get_object_or_404(Person, userid=request.user.username)
    
    contract = TAContract.objects.filter(posting=posting, application__person__userid=userid)
    #  If you have at least one contract for this posting, return the first one that matches.
    if contract.count() > 0:
        contract = contract[0]
        application = TAApplication.objects.get(person__userid=userid, posting=posting)
    #  Otherwise, someone is probably trying to guess/make up a URL, or the contract has been deleted since they
    #  got the URL.
    else:
        return ForbiddenResponse(request)

    courses = TACourse.objects.filter(contract=contract)
    total = contract.total_bu()
    bu = contract.bu()
    
    #this could be refactored used in multiple places
    pp = posting.payperiods()
    pdead = contract.deadline
    salary_sem = (total*contract.pay_per_bu)
    schol_sem = (bu*contract.scholarship_per_bu)
    salary_sem_out = _format_currency(salary_sem)
    schol_sem_out = _format_currency(schol_sem)
    salary_bi = _format_currency(salary_sem / pp)
    schol_bi = _format_currency(schol_sem / pp)
    today = datetime.date.today()
    deadline_passed = pdead < today

    if request.method == "POST":
        form = TAAcceptanceForm(request.POST, instance=contract)
        if form.is_valid():
            contract = form.save(commit=False)
            contract.posting = posting
            contract.application = application
            contract.sin = request.POST['sin']
            #contract.status = request.POST['status']
            grad = GradStudent.objects.filter(person=person)           
            if grad.count()>0:
                grad[0].config['sin'] = request.POST['sin']
           
            if "reject" in request.POST:
                contract.status = 'REJ'
                contract.config['rejected_date'] = datetime.datetime.now()
                if 'send_notify' in posting.config and posting.config['send_notify']:
                    contract.send_notify('rejected')
                l = LogEntry(userid=request.user.username,
                        description="TA Rejected for %s (%s in %s)." % (contract.application.person.userid, contract.application.posting.semester, contract.application.posting.unit),
                        related_object=contract.application)
                l.save()
            elif "accept" in request.POST:
                contract.status = 'ACC'
                contract.config['accepted_date'] = datetime.datetime.now()
                if 'send_notify' in posting.config and posting.config['send_notify']:
                    contract.send_notify('accepted')
            contract.save()
            messages.success(request, "Successfully %s the offer." % (contract.get_status_display()))
            
            # Do this after the save, just in case something went wrong during saving:
            if "accept" in request.POST:
                contract.email_contract()
                l = LogEntry(userid=request.user.username,
                        description="TA Accepted and contract sent to %s email %s." % (contract.application.person.userid, contract.application.person.email()),
                        related_object=contract.application)
                l.save()
                messages.info(request, "You should be receiving an email with your contract attached.")

            ##not sure where to redirect to...so currently redirects to itself
            return HttpResponseRedirect(reverse('ta:accept_contract', args=(post_slug,userid)))
    else:   
        form = TAContractForm(instance=contract) 

    context = { 'contract':contract, 
                'courses':courses,
                'pay':_format_currency(contract.pay_per_bu),
                'scholarship':_format_currency(contract.scholarship_per_bu),
                'salary_bi':salary_bi,
                'schol_bi':schol_bi,
                'salary_sem':salary_sem_out,
                'schol_sem':schol_sem_out,
                'total':total,
                'acc_deadline': pdead,
                'form':form,
                'preview': preview,
                'deadline_passed': deadline_passed,
            }
    return render(request, 'ta/accept.html', context)

@requires_role("TAAD")
def view_contract(request, post_slug, userid):
    #contract = get_object_or_404(TAContract, pk=contract_id)
    #ta courses get all courses with contract_id
    #courses = TACourse.objects.filter(contract=contract)
    #contract.pay_per_bu = format_currency(contract.pay_per_bu)
    #contract.scholarship_per_bu = format_currency(contract.scholarship_per_bu)
    
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')
    contract = get_object_or_404(TAContract, posting=posting, application__person__userid=userid)
    courses = TACourse.objects.filter(contract=contract)
    
    total = contract.total_bu()
    bu = contract.bu()
    
    pp = posting.payperiods()
    salary_sem = (total*contract.pay_per_bu)
    schol_sem = (bu*contract.scholarship_per_bu)
    salary_sem_out = _format_currency(salary_sem)
    schol_sem_out = _format_currency(schol_sem)
    salary_bi = _format_currency(salary_sem / pp)
    schol_bi = _format_currency(schol_sem / pp)


    context =   {'posting': posting,
                 'contract':contract,
                 'courses':courses,
                 'pay':_format_currency(contract.pay_per_bu),
                 'scholarship':_format_currency(contract.scholarship_per_bu),
                 'salary_bi':salary_bi,
                 'schol_bi':schol_bi,
                 'salary_sem':salary_sem_out,
                 'schol_sem':schol_sem_out,
                 'total':total
                 }
     
    
    
    return render(request, 'ta/view_contract.html', context)

@requires_role("TAAD")
def view_form(request, post_slug, userid):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    contract = get_object_or_404(TAContract, posting=posting, application__person__userid=userid)
    response = HttpResponse(content_type="application/pdf")
    filename =  "%s_%s_%s_%s.pdf" % (contract.application.person.last_name, contract.application.person.first_name , 
                                                                              contract.application.person.emplid, posting.slug)
    response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
    ta_form(contract, response)
    return response

@requires_role("TAAD")
def contracts_forms(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    contracts = TAContract.objects.filter(posting=posting, status__in=['ACC', 'SGN']).order_by('application__person__last_name', 'application__person__first_name')
    response = HttpResponse(content_type="application/pdf")
    response['Content-Disposition'] = 'inline; filename="%s.pdf"' % (posting.slug)
    ta_forms(contracts, response)
    return response

@requires_role("TAAD")
def new_contract(request, post_slug):
    """
    Create a new contract for this person and redirect to edit it.
    """
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)

    existing = set(c.application_id for c in TAContract.objects.filter(posting=posting).select_related('application'))
    queryset = TAApplication.objects.filter(posting=posting).exclude(id__in=existing).order_by('person')
    application_choices = [(a.id, a.person.name()) for a in queryset if a not in existing]
    
    if request.method == 'POST':
        form = NewTAContractForm(request.POST)
        form.fields['application'].choices = application_choices
        form.fields['application'].queryset = queryset
        
        if form.is_valid():
            app = form.cleaned_data['application']
            contract = TAContract(created_by=request.user.username)
            contract.first_assign(app, posting)
            return HttpResponseRedirect(reverse('ta:edit_contract', kwargs={'post_slug': posting.slug, 'userid': app.person.userid}))
    
    return HttpResponseRedirect(reverse('ta:all_contracts', args=(post_slug,)))


def _lab_or_tutorial( courseDescription ):
    if courseDescription.labtut:
        return " (+%.2f BU)" % LAB_BONUS_DECIMAL
    else:
        return ""
 

@requires_role("TAAD")
def edit_contract(request, post_slug, userid):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')
        
    course_choices = [('','---------')] + [(c.id, c.name()) for c in posting.selectable_offerings()]
    position_choices = [(a.id, "%s (%s)" % (a.position_number, a.title)) for a in Account.objects.filter(unit=posting.unit, hidden=False)]
    description_choices = [('', '---------')] + [(d.id, d.description + _lab_or_tutorial(d) ) 
                                for d in CourseDescription.objects.filter(unit=posting.unit, hidden=False)]
    
    #number of course form to populate
    num = 3
    contract = TAContract.objects.filter(posting=posting, application__person__userid=userid)
    if contract.count() > 0:
        contract = contract[0]
        application = contract.application
        cnum = contract.tacourse_set.all().count()
        if cnum > num:
            num = 0
        else:
            num = num - contract.tacourse_set.all().count()
        old_status = contract.get_status_display()
        if contract.status not in ['NEW', 'OPN']:
            # after editing, revert to open
            contract.status = 'OPN'
        editing = True
    else:
        # creating new contract
        contract = TAContract()
        application = TAApplication.objects.get(person__userid=userid, posting=posting)
        old_status = None
        editing = False
    
    TACourseFormset = inlineformset_factory(TAContract, TACourse, extra=num, can_delete=editing, form=TACourseForm, formset=BaseTACourseFormSet)
    formset = TACourseFormset(instance=contract)
    if request.method == "POST":
        form = TAContractForm(request.POST, instance=contract)
        
        if request.is_ajax():
            if('appt_cat' in request.POST):
                index = posting.cat_index(request.POST['appt_cat'])
                results = posting.salary()[index] + ',' + posting.scholarship()[index] + ',' + str(posting.accounts()[index])
                return HttpResponse(results)
            if('course' in request.POST):
                course = request.POST['course']
                co = get_object_or_404(CourseOffering, pk=course)
                req_bu = posting.required_bu(co)
                assigned_bu = posting.assigned_bu(co)
                #subtracting assigned_bu from req_bu
                if(assigned_bu > req_bu):
                    req_bu = 0.0
                else:
                    req_bu -= assigned_bu
                return HttpResponse(str(req_bu))
        elif form.is_valid():
            contract = form.save(commit=False)
            formset = TACourseFormset(request.POST, instance=contract)
            if formset.is_valid():
                #If course isn't in applicants prefered courses, add it with rank 0
                course_prefs = [c.course for c in CoursePreference.objects.filter(app=application)]
                for form in formset:
                    if 'course' not in form.cleaned_data:
                        continue
                    offering = form.cleaned_data['course']
                    if offering.course not in course_prefs:
                        new_course_pref = CoursePreference(app=application, course=offering.course, taken='YES', exper='FAM', rank=0)
                        new_course_pref.save()

                contract.application = application
                contract.posting = posting
                contract.created_by = request.user.username
                
                #create news item
                # if contract.status == "OPN":
                person = application.person
                
                offer_url = reverse('ta:accept_contract', kwargs={'post_slug': post_slug, 'userid': userid})
                from_user = posting.contact()
                if contract.status == 'OPN':
                    _create_news(person, offer_url, from_user, contract.deadline, contract.total_bu())
                
                grad = GradStudent.objects.filter(person=person)           
                if grad.count()>0:
                    grad[0].config['sin'] = request.POST['sin']
                
                formset.save()
                contract.save()

                if not editing:
                    messages.success(request, "Created TA Contract for %s for %s." % (contract.application.person, posting))
                else:
                    messages.success(request, "Edited TA Contract for %s for %s." % (contract.application.person, posting))
                return HttpResponseRedirect(reverse('ta:all_contracts', args=(post_slug,)))
    else:
        form = TAContractForm(instance=contract) 
        formset = TACourseFormset(instance=contract)
        if not editing:
            initial={'sin': application.sin,
                     'appt_category': application.category,
                     'position_number': posting.accounts()[posting.cat_index(application.category)],
                     'appointment_start': posting.start(),
                     'appointment_end': posting.end(),
                     'pay_start': posting.payroll_start(),
                     'pay_end': posting.payroll_end(),
                     'deadline': posting.deadline()
                     }
                     
            form = TAContractForm(initial=initial)

    form.fields['position_number'].choices = position_choices       
    for f in formset:
        f.fields['course'].choices = course_choices
        f.fields['description'].choices = description_choices
    
    ids_of_descriptions_with_labs = json.dumps([d.id for d in CourseDescription.objects.filter(unit=posting.unit, hidden=False, labtut=True)])
    
    context = {'form': form, 'formset': formset, 'posting': posting, 'editing': editing,
               'old_status': old_status, 'contract': contract, 'application': application,
               'userid': userid, 'LAB_BONUS': LAB_BONUS, 'ids_of_descriptions_with_labs':ids_of_descriptions_with_labs}
    return render(request, 'ta/edit_contract.html',context)

def _copy_posting_defaults(source, destination):
    """
    Copy some defaults from source posting to the destination
    """
    destination.set_salary(source.salary())
    destination.set_scholarship(source.scholarship())
    destination.set_accounts(source.accounts())
    destination.set_bu_defaults(source.bu_defaults())
    destination.set_payperiods(source.payperiods())
    destination.set_contact(source.contact().id)
    destination.set_offer_text(source.offer_text())
    destination.set_tssu_link(source.tssu_link())       
    # TODO: also copy Skill values

@requires_role("TAAD")
def edit_posting(request, post_slug=None):
    unit_choices = [(u.id, str(u)) for u in request.units]
    account_choices = [(a.id, "%s (%s)" % (a.position_number, a.title)) for a in Account.objects.filter(unit__in=request.units, hidden=False).order_by('title')]

    today = datetime.date.today()
    if post_slug:
        semester_choices = [(s.id, str(s)) for s in Semester.objects.all().order_by('start')]
    else:
        semester_choices = [(s.id, str(s)) for s in Semester.objects.filter(start__gt=today).order_by('start')]
    # TODO: display only relevant semester/unit offerings (with AJAX magic)
    offerings = CourseOffering.objects.filter(owner__in=request.units, semester__end__gte=datetime.date.today()).select_related('course')
    excluded_choices = list(set((("%s (%s)" % (o.course,  o.title), o.course_id) for o in offerings)))
    excluded_choices.sort()
    excluded_choices = [(cid,label) for label,cid in excluded_choices]

    if post_slug:
        # editing existing
        posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
        if posting.unit not in request.units:
            ForbiddenResponse(request, 'You cannot access this posting')
        editing = True
    else:
        # creating new
        posting = TAPosting()
        editing = False

        # populate from previous semester if possible
        old_postings = TAPosting.objects.filter(unit__in=request.units).order_by('-semester')
        if old_postings:
            old = old_postings[0]
            _copy_posting_defaults(old, posting)
        else:
            # heuristic default: non-lecture sections, except distance, are excluded
            default_exclude = set((o.course_id for o in offerings.filter(component="SEC").exclude(section__startswith="C")))
            posting.config['excluded'] = default_exclude
            posting.config['contact'] = Person.objects.get(userid=request.user.username).id

    contact_choices = [(r.person.id, r.person.name()) for r in Role.objects_fresh.filter(unit__in=request.units)]
    current_contact = posting.contact()
    if current_contact:
        contact_choices.append((current_contact.id, current_contact.name()))
    contact_choices = list(set(contact_choices))
    # Let's sort these choices by name at least.
    contact_choices = sorted(contact_choices, key=lambda name: name[1])
    
    if request.method == "POST":
        form = TAPostingForm(data=request.POST, instance=posting)
        form.fields['unit'].choices = unit_choices
        form.fields['semester'].choices = semester_choices
        form.fields['excluded'].choices = excluded_choices
        form.fields['contact'].choices = contact_choices
        for f in form.fields['accounts'].fields:
            f.choices = account_choices
        for w in form.fields['accounts'].widget.widgets:
            w.choices = account_choices
        if form.is_valid():
            form.instance.slug = None
            form.save()
            found_skills = set()
            for s in form.cleaned_data['skills']:
                s.posting = form.instance
                s.save()
                found_skills.add(s.id)
            
            # if any skills were dropped, remove them
            Skill.objects.filter(posting=form.instance).exclude(id__in=found_skills).delete()
            
            l = LogEntry(userid=request.user.username,
                  description="Edited TAPosting for %s in %s." % (form.instance.unit, form.instance.semester),
                  related_object=form.instance)
            l.save()
            if editing:
                messages.success(request, "Edited TA posting for %s in %s." % (form.instance.unit, form.instance.semester))
            else:
                messages.success(request, "Created TA posting for %s in %s." % (form.instance.unit, form.instance.semester))
            return HttpResponseRedirect(reverse('ta:view_postings', kwargs={}))
    else:
        form = TAPostingForm(instance=posting)
        form.fields['unit'].choices = unit_choices
        form.fields['semester'].choices = semester_choices
        form.fields['excluded'].choices = excluded_choices
        form.fields['contact'].choices = contact_choices
        for f in form.fields['accounts'].fields:
            f.choices = account_choices
        for w in form.fields['accounts'].widget.widgets:
            w.choices = account_choices

    context = {'form': form, 'editing': editing, 'posting': posting}
    return render(request, 'ta/edit_posting.html', context)


@requires_role("TAAD")
def posting_admin(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    default_visible = bu_rules.does_bu_strategy_involve_defaults(posting.semester, posting.unit) 
    # check any delete courses with BU assigned
    cancel_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit, component="CAN")
    all_assignments = TACourse.objects.filter(contract__posting=posting).select_related('course', 'contract')
    message = []
    
    for o in cancel_offerings:        
        for crs in all_assignments:
            if crs.course == o and crs.contract.bu() > 0:
                message.append('Course ' + o.name() +' was cancelled, but still have BU assigned')
        
    context = {'posting': posting, 
               'default_visible': default_visible}
    messages.add_message(request, messages.WARNING, ', '.join(message))

    context = {'posting': posting, 
               'default_visible': default_visible }
    return render(request, 'ta/posting_admin.html', context)


@requires_role("TAAD")
def bu_formset(request, post_slug):
    """
    AJAX method to build the formset for a particular level
    
    Called in edit_bu.html to dynmically change formset as selected
    """
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')
    
    if 'level' not in request.GET:
        return ForbiddenResponse(request, 'must give level')
    level = request.GET['level']
    
    # populate existing values if exist
    initial=[]
    defaults = posting.bu_defaults()
    if level in defaults:
        initial = [{'students': s, 'bus': b} for s,b in defaults[level]]
    formset = BUFormSet(prefix="set"+level, initial=initial)
    
    context = {'level': level, 'formset': formset}
    return render(request, 'ta/bu_formset.html', context)
    

@requires_role("TAAD")
def edit_bu(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if posting.unit not in request.units:
        ForbiddenResponse(request, 'You cannot access this page')

    formset = None # used in bu_formset.html as defaults if present; AJAX magic if not
    level = None
    if request.method == "POST":
        form = TAPostingBUForm(request.POST)
        if form.is_valid():
            level = form.cleaned_data['level']
            formset = BUFormSet(request.POST, prefix="set"+level)
            if formset.is_valid():
                bus = [(d['students'], d['bus']) for d in formset.cleaned_data if 'bus' in d and 'students' in d]
                bus.sort()
                defaults = posting.bu_defaults()
                defaults[level] = bus
                posting.set_bu_defaults(defaults)
                posting.save()
                
                l = LogEntry(userid=request.user.username,
                  description="Edited BU defaults for %s, level %s." % (posting, level),
                  related_object=posting)
                l.save()
                messages.success(request, "Updated BU defaults for %s, %s-level." % (posting, level))
    else:
        form = TAPostingBUForm()

    context = {'form': form, 'formset': formset, 'posting': posting, 'level': level}
    return render(request, 'ta/edit_bu.html',context)


def _by_start_semester(gradstudent):
    "Used to find the grad program with most recent start semester"
    if gradstudent.start_semester:
        return gradstudent.start_semester.name
    else:
        return 'ZZZZ'

@requires_role("TAAD")
def generate_csv(request, post_slug):
    # An even shorter Campus name, for smaller columns in the CSV.
    CAMPUS_CHOICES_SHORTENED = (
        ('BRNBY', 'BBY'),
        ('SURRY', 'SRY'),
        ('VANCR', 'VCR'),
        ('OFFST', 'OFF'),
        ('GNWC', 'GNW'),
        ('METRO', 'OTHR'),
    )
    CAMPUSES_SHORTENED = dict(CAMPUS_CHOICES_SHORTENED)
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).exclude(component='CAN').select_related('course')
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]

    # collect all course preferences in a sensible way
    course_prefs = {}
    prefs = CoursePreference.objects.filter(app__posting=posting).exclude(rank=0).order_by('app__person').select_related('app', 'course')
    for cp in prefs:
        a = cp.app
        c = cp.course
        if a not in course_prefs:
            course_prefs[a] = {}
        course_prefs[a][c] = cp
    
    # generate CSV
    filename = str(posting.slug) + '.csv'
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
    csvWriter = csv.writer(response)
    
    #First csv row: all the course names
    off = ['Rank', 'Name', 'SFUID', 'Email', 'Categ', 'Program (Reported)', 'Program (System)', 'Status', 'Supervisor (Reported)', 'Supervisor (System)', 'Unit', 'Start Sem', 'BU',
           'Campus', 'Assigned Course(s)', 'Assigned BUs'] + [str(o.course) + ' ' + str(o.section) for o in offerings]
    csvWriter.writerow(off)
    
    # next row: campuses
    off = ['']*16 + [str(CAMPUSES_SHORTENED[o.campus]) for o in offerings]
    csvWriter.writerow(off)
    
    apps = TAApplication.objects.filter(posting=posting).order_by('person')
    for app in apps:
        rank = 'R%d' % app.rank
        system_program = ''
        startsem = ''
        status = ''
        unit = ''
        # grad program info
        gradstudents = GradStudent.get_canonical(app.person, app.posting.semester)
        if len(gradstudents) == 1:
            gs = gradstudents[0]
            system_program = gs.program.label
            status = gs.get_current_status_display()
            unit = gs.program.unit.label
            if gs.start_semester:
                startsem = gs.start_semester.name
            else:
                startsem = ''
        elif len(gradstudents) > 1:
            system_program = "Multiple"
            status = "*"
            unit = "*"
            startsem = "*"
        
        campuspref = ''
        for cp in CampusPreference.objects.filter(app=app):
            if cp.pref == 'PRF':
                campuspref += cp.campus[0].upper()
            elif cp.pref == 'WIL':
                campuspref += cp.campus[0].lower()

        # Get all TAContracts that match this posting and application, then the matching TACourses
        # so we can find out if a course/courses have been assigned to this TA

        assigned_courses = ''
        assigned_bus = ''
        ta_contracts = TAContract.objects.filter(posting=posting, application=app).exclude(status__in=['CAN', 'REJ'])
        if len(ta_contracts) > 0:
            ta_courses = TACourse.objects.filter(contract__in=ta_contracts)
            if len(ta_courses) > 0:
                assigned_courses = ', '.join([tacourse.course.name() for tacourse in ta_courses])
                assigned_bus = sum([t.total_bu for t in ta_courses])

        supervisorlist = app.coursys_supervisor_display()
        row = [rank, app.person.sortname(), app.person.emplid, app.person.email(), app.category, app.get_current_program_display(), system_program, status, app.supervisor, supervisorlist, unit, startsem,
               app.base_units, campuspref, assigned_courses, assigned_bus]
        
        for off in offerings:
            crs = off.course
            if app in course_prefs and crs in course_prefs[app]:
                pref = course_prefs[app][crs]
                row.append(pref.rank)
            else:
                row.append(None)
            
        csvWriter.writerow(row)
    
    return response

@requires_role("TAAD")
def generate_csv_detail(request, post_slug):
    # An even shorter Campus name, for smaller columns in the CSV.
    CAMPUS_CHOICES_SHORTENED = (
        ('BRNBY', 'BBY'),
        ('SURRY', 'SRY'),
        ('VANCR', 'VCR'),
        ('OFFST', 'OFF'),
        ('GNWC', 'GNW'),
        ('METRO', 'OTHR'),
    )
    CAMPUSES_SHORTENED = dict(CAMPUS_CHOICES_SHORTENED)
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).exclude(component='CAN').select_related('course')
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]

    # collect all course preferences in a sensible way
    course_prefs = {}
    prefs = CoursePreference.objects.filter(app__posting=posting).exclude(rank=0).order_by('app__person').select_related('app', 'course')
    for cp in prefs:
        a = cp.app
        c = cp.course
        if a not in course_prefs:
            course_prefs[a] = {}
        course_prefs[a][c] = cp
    
    # generate CSV
    filename = str(posting.slug) + '.csv'
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s"' % (filename)
    csvWriter = csv.writer(response)
    
    #First csv row: all the course names
    off = ['Rank', 'Name', 'SFUID', 'Email', 'Type', 'Year/Sem', 'Categ', 'Program (Reported)', 'Program (System)', 'Status', 'Supervisor (Reported)', 'Supervisor (System)', 'TA Experience', 'Unit', 'Start Sem', 'BU',
           'Campus', 'Course preference comment', 'Assigned Course(s)', 'Assigned Total BUs'] + [str(o.course) + ' ' + str(o.section) for o in offerings]
    csvWriter.writerow(off)
    
    # next row: campuses
    off = ['']*20 + [str(CAMPUSES_SHORTENED[o.campus]) for o in offerings]
    csvWriter.writerow(off)
    
    # collect all grad program who apply ta posting
    personids = TAApplication.objects.filter(posting=posting).values_list('person_id', flat=True)
    active_gs = GradStudent.objects.filter(person_id__in=personids, current_status__in=STATUS_REAL_PROGRAM) \
                .select_related('program__unit')

    # collect all campus preferences in a sensible way
    allcp = CampusPreference.objects.filter(app__posting=posting)
    
    apps = TAApplication.objects.filter(posting=posting).order_by('person')
    for app in apps:
        rank = 'R%d' % app.rank
        
        system_program = ''
        startsem = ''
        status = ''
        unit = ''
        gptype = '' 
        gpyear = '' 

        # grad program info        
        for g in active_gs:
                if g.person_id == app.person_id:
                    if 'phd' in g.program.slug:
                        gptype =  'P1'
                        gpyear = g.year_as_of()
                    elif 'profmsc' in g.program.slug:
                        gptype = 'P3'
                        gpyear = g.semester_as_of()
                    elif 'msc' in g.program.slug:
                        gptype = 'P2'
                        gpyear = g.year_as_of()
                    else:
                        gptype = 'P4'

                    system_program = g.program.label
                    status = g.get_current_status_display()
                    unit = g.program.unit.label
                    if g.start_semester:
                        startsem = g.start_semester.name
                    else:
                        startsem = ''
                    
                    break

        campuspref = ''
        for cp in allcp:
            if cp.app == app:
                if cp.pref == 'PRF':
                    campuspref += cp.campus[0].upper()
                elif cp.pref == 'WIL':
                    campuspref += cp.campus[0].lower()

        # Get all TAContracts that match this posting and application, then the matching TACourses
        # so we can find out if a course/courses have been assigned to this TA

        assigned_courses = ''
        assigned_bus = ''
        ta_contracts = TAContract.objects.filter(posting=posting, application=app).exclude(status__in=['CAN', 'REJ'])
        if len(ta_contracts) > 0:
            ta_courses = TACourse.objects.filter(contract__in=ta_contracts).select_related('course')
            if len(ta_courses) > 0:
                assigned_courses = ', '.join([tacourse.course.name() for tacourse in ta_courses])
                assigned_bus = sum([t.total_bu for t in ta_courses])

        supervisorlist = app.coursys_supervisor_display()
                
        row = [rank, app.person.sortname(), app.person.emplid, app.person.email(), gptype, gpyear, app.category, app.get_current_program_display(), system_program, status, app.supervisor, supervisorlist, app.past_experience_display(), unit, startsem,
               app.base_units, campuspref, app.preference_comment, assigned_courses, assigned_bus]
        
        for off in offerings:
            crs = off.course
            if app in course_prefs and crs in course_prefs[app]:
                pref = course_prefs[app][crs]
                row.append(pref.rank)
            else:
                row.append(None)
            
        csvWriter.writerow(row)
    
    return response

@requires_role("TAAD")
def generate_csv_by_course(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit).exclude(component='CAN').select_related('course')
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl]
    
    # collect all course preferences in a sensible way
    prefs = CoursePreference.objects.filter(app__posting=posting).exclude(rank=0).order_by('app__person').select_related('app', 'course')
    
    # generate CSV
    filename = str(posting.slug) + '_by_course.csv'
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s"'% filename
    csvWriter = csv.writer(response)
    
    #First csv row: all the course names
    off = ['Rank', 'Name', 'Student ID', 'Email', 'Category', 'Program', 'Max BU']
    extra_questions = []
    if 'extra_questions' in posting.config and len(posting.config['extra_questions']) > 0:
        for question in posting.config['extra_questions']:
            off.append(question[0:75])
            extra_questions.append(question)

    offering_rows = []
    courseids = []
    for offering in offerings: 
        offering_rows.append([offering.course.subject + " " + offering.course.number + " " + offering.section])
        courseids.append(offering.course_id)
        applications_for_this_offering = [pref.app for pref in prefs if 
            (pref.course.number == offering.course.number and pref.course.subject == offering.course.subject)]
        for app in applications_for_this_offering:
            rank = 'R%d' % app.rank      
            row = [rank, app.person.sortname(), app.person.emplid, app.person.email(), app.category, app.get_current_program_display(), app.base_units]
            
            if 'extra_questions' in posting.config and len(posting.config['extra_questions']) > 0 and 'extra_questions' in app.config:
                for question in extra_questions:
                    try:
                        row.append(app.config['extra_questions'][question])
                    except KeyError:
                        row.append("")
                for question in app.config['extra_questions']:
                    if not question in extra_questions:
                        off.append(question[0:75])
                        extra_questions.append(question)
                        row.append(app.config['extra_questions'][question])
            
            offering_rows.append(row)
        offering_rows.append([])

    # collect those applicants without choose any course
    prefsappid = CoursePreference.objects.filter(app__posting=posting, course_id__in=courseids).exclude(rank=0).order_by('app__person').select_related('app', 'course').values_list('app_id', flat=True)
    noprefs = TAApplication.objects.filter(posting=posting).exclude(id__in=prefsappid)

    noprefs_rows = []
    for nopref in noprefs: 
        noprefs_rows.append(['', nopref.person.sortname(), nopref.person.emplid, nopref.person.email(), nopref.category, nopref.get_current_program_display(), nopref.base_units])

    csvWriter.writerow(off)
    for row in offering_rows:
        csvWriter.writerow(row)
    
    if len(noprefs) > 0:
        csvWriter.writerow(['Below applicants have no course preference or selected course was canceled'])
        for row in noprefs_rows:
            csvWriter.writerow(row)
    return response
    
@requires_role("TAAD")
def view_financial(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)    
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit)
    # ignore excluded courses
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl and posting.ta_count(o) > 0]
    excluded = [o for o in all_offerings if o.course_id in excl]
    
    (bu, pay, ta) = posting.all_total()
    info = {'course_total': len(offerings), 'bu_total': bu, 'pay_total': pay, 'ta_count': ta}
    
    context = {'posting': posting, 'offerings': offerings, 'excluded': excluded, 'info': info}
    return render(request, 'ta/view_financial.html', context)


@requires_role("TAAD")
def download_financial(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    all_offerings = CourseOffering.objects.filter(semester=posting.semester, owner=posting.unit)
    # ignore excluded courses
    excl = set(posting.excluded())
    offerings = [o for o in all_offerings if o.course_id not in excl and posting.ta_count(o) > 0]
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'inline; filename="%s-financials-%s.csv"' % \
                                      (post_slug, datetime.datetime.now().strftime('%Y%m%d'))
    writer = csv.writer(response)
    writer.writerow(['Offering', 'Instructor(s)', 'Enrollment', 'Campus', 'Number of TAs', 'Assigned BU',
                     'Total Amount'])
    for o in offerings:
        writer.writerow([o.name(), o.instructors_str(), '(%s/%s)' % (o.enrl_tot, o.enrl_cap), o.get_campus_display(),
                         posting.ta_count(o), posting.assigned_bu(o), locale.currency(float(posting.total_pay(o)))])
    return response

def _contact_people(posting, statuses):
    """
    The set of people to be contacted with the given statuses in the given posting.
    """
    contracts = TAContract.objects.filter(posting=posting, status__in=statuses).select_related('application__person')
    people = set((c.application.person for c in contracts))
    if '_APPLIC' in statuses:
        # they want applicants
        apps = TAApplication.objects.filter(posting=posting, late=False).select_related('person')
        people |= set((app.person for app in apps))
    if '_LATEAPP' in statuses:
        # they want applicants
        apps = TAApplication.objects.filter(posting=posting, late=True).select_related('person')
        people |= set((app.person for app in apps))
    return people

@requires_role("TAAD")
def contact_tas(request, post_slug):
    posting = get_object_or_404(TAPosting, slug=post_slug, unit__in=request.units)
    if request.method == "POST":
        form = TAContactForm(request.POST)
        if form.is_valid():
            people = _contact_people(posting, form.cleaned_data['statuses'])
            from_person = Person.objects.get(userid=request.user.username)
            if 'url' in form.cleaned_data and form.cleaned_data['url']:
                url = form.cleaned_data['url']
            else:
                url = ''
            # message each person
            count = 0
            for person in people:
                n = NewsItem(user=person, author=from_person, course=None, source_app='ta_contract',
                             title=form.cleaned_data['subject'], content=form.cleaned_data['text'], url=url)
                n.save()
                count += 1
            
            messages.success(request, "Message sent to %i TAs." % (count))
            return HttpResponseRedirect(reverse('ta:posting_admin', kwargs={'post_slug': posting.slug}))
    
    elif 'statuses' in request.GET:
        statuses = request.GET['statuses'].split(',')
        people = _contact_people(posting, statuses)
        emails = [p.full_email() for p in people]
        
        resp = HttpResponse(content_type="application/json")
        data = {'contacts': ", ".join(emails)}
        json.dump(data, resp, indent=1)
        return resp
    else:
        form = TAContactForm()

    context = {'posting': posting, 'form': form}
    return render(request, 'ta/contact_tas.html', context) 

@requires_course_instr_by_slug 
def ta_offers(request, course_slug):
    offering = get_object_or_404(CourseOffering, slug=course_slug)
    contracts = [(ta.contract, ta.bu) for ta in TACourse.objects.filter(course=offering).exclude(contract__status='NEW')]
    context = {'contracts': contracts, 'course': offering}
    return render(request, 'ta/view_tas.html', context)


@requires_role("TAAD")
def descriptions(request):
    descriptions = CourseDescription.objects.filter(unit__in=request.units, hidden=False).select_related('unit')
    context = {'descriptions': descriptions, 'LAB_BONUS': LAB_BONUS}
    return render(request, 'ta/descriptions.html', context)

@requires_role("TAAD")
def new_description(request):
    unit_choices = [(u.id, str(u)) for u in request.units]
    if request.method == 'POST':
        form = CourseDescriptionForm(request.POST)
        form.fields['unit'].choices = unit_choices
        if form.is_valid():
            desc = form.save(commit=False)
            desc.hidden = False
            desc.save()
            
            messages.success(request, "Created course description '%s'." % (desc.description))
            l = LogEntry(userid=request.user.username,
                  description="Created contract description '%s' in %s." % (desc.description, desc.unit.label),
                  related_object=desc)
            l.save()
            return HttpResponseRedirect(reverse('ta:descriptions', kwargs={}))
            
    else:
        form = CourseDescriptionForm()
        form.fields['unit'].choices = unit_choices
    context = {'form': form}
    return render(request, 'ta/new_description.html', context)


@requires_role("TAAD")
def edit_description(request, description_id):
    description = get_object_or_404(CourseDescription, pk=description_id, unit__in=request.units)
    if request.method == 'POST':
        form = CourseDescriptionForm(request.POST, instance=description)
        if form.is_valid():
            description = form.save()
            messages.add_message(request,
                                 messages.SUCCESS,
                                 'Description was modified')
            l = LogEntry(userid=request.user.username,
                         description="Modified description %s" % description.description,
                         related_object=description)
            l.save()
            return HttpResponseRedirect(reverse('ta:descriptions'))
    else:
        form = CourseDescriptionForm(instance=description)
    return render(request, 'ta/edit_description.html', {'form': form})


@requires_role("TAAD")
def delete_description(request, description_id):
    description = get_object_or_404(CourseDescription, pk=description_id, unit__in=request.units)
    if request.method == 'POST':
        # Descriptions are actual basically text, we will allow them to delete them.
        description.delete()
        messages.success(request, 'Deleted description %s' % description.description)
        l = LogEntry(userid=request.user.username,
                     description="Deleted description: %s" % description.description,
                     related_object=description)
        l.save()
    return HttpResponseRedirect(reverse('ta:descriptions'))


@requires_role("TAAD")
def add_edit_ta_contract_email(request):
    #  It's probably safe to assume everyone but sysadmins are only TA admins for a single school.  Anyone with more
    #  than one role most likely won't be actually adding/editing this text.
    unit = list(request.units)[0]
    instance = TAContractEmailText.objects.filter(unit=unit).first()
    if request.method == 'POST':
        form = TAContractEmailTextForm(request.POST, instance=instance)
        if form.is_valid():
            text = form.save(commit=False)
            text.unit = unit
            text.save()
            messages.success(request, 'Email text successfully changed.')
            l = LogEntry(userid=request.user.username,
                         description='Added/Modified TA Contract Email Text for %s.' % text.unit.label,
                         related_object=text)
            l.save()
            return HttpResponseRedirect(reverse('ta:view_postings'))
    else:
        form = TAContractEmailTextForm(instance=instance)
    return render(request, 'ta/edit_contract_email.html', {'form': form})

@requires_role("TAAD")
def ta_exclude_choice(request, post_slug=None):
    offerings = CourseOffering.objects.filter(owner__in=request.units, semester_id=request.GET.get("semester_id")).select_related('course')
    excluded_choices = list(set((("%s (%s)" % (o.course,  o.title), o.course_id) for o in offerings)))
    excluded_choices.sort()
    response = json.dumps(excluded_choices)        
    mimetype = "application/json"
    return HttpResponse(response, mimetype)

