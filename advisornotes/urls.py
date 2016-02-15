from django.conf.urls import url
from courselib.urlparts import UNIT_COURSE_SLUG, NOTE_ID, SEMESTER, COURSE_SLUG, ARTIFACT_SLUG, USERID_OR_EMPLID, \
    NONSTUDENT_SLUG, UNIT_SLUG

advisornotes_patterns = [ # prefix /advising/
    url(r'^$', 'advisornotes.views.advising'),
    #url(r'^new_notes/$', 'advisornotes.views.rest_notes'),
    url(r'^hide_note$', 'advisornotes.views.hide_note'),
    url(r'^note_search$', 'advisornotes.views.note_search'),
    url(r'^sims_search$', 'advisornotes.views.sims_search'),
    url(r'^sims_add$', 'advisornotes.views.sims_add_person'),
    url(r'^visits', 'advisornotes.views.all_visits'),

    url(r'^courses/$', 'advisornotes.views.view_courses'),
    url(r'^courses/' + UNIT_COURSE_SLUG + '/new$', 'advisornotes.views.new_artifact_note'),
    url(r'^courses/' + UNIT_COURSE_SLUG + '/moreinfo$', 'advisornotes.views.course_more_info'),
    url(r'^courses/' + UNIT_COURSE_SLUG + '/' + NOTE_ID + '/edit$', 'advisornotes.views.edit_artifact_note'),
    url(r'^courses/' + UNIT_COURSE_SLUG + '/$', 'advisornotes.views.view_course_notes'),
    url(r'^offerings/$', 'advisornotes.views.view_course_offerings'),
    url(r'^offerings/semesters$', 'advisornotes.views.view_all_semesters'),
    url(r'^offerings/' + SEMESTER + '$', 'advisornotes.views.view_course_offerings'),
    url(r'^offerings/' + COURSE_SLUG + '/new$', 'advisornotes.views.new_artifact_note'),
    url(r'^offerings/' + COURSE_SLUG + '/$', 'advisornotes.views.view_offering_notes'),
    url(r'^offerings/' + COURSE_SLUG + '/' + NOTE_ID + '/edit$', 'advisornotes.views.edit_artifact_note'),
    url(r'^artifacts/$', 'advisornotes.views.view_artifacts'),
    url(r'^artifacts/' + ARTIFACT_SLUG + '/new$', 'advisornotes.views.new_artifact_note'),
    url(r'^artifacts/' + ARTIFACT_SLUG + '/$', 'advisornotes.views.view_artifact_notes'),
    url(r'^artifacts/' + ARTIFACT_SLUG + '/' + NOTE_ID + '/edit$', 'advisornotes.views.edit_artifact_note'),
    url(r'^new_artifact$', 'advisornotes.views.new_artifact'),
    url(r'^artifacts/' + ARTIFACT_SLUG + '/edit$', 'advisornotes.views.edit_artifact'),
    url(r'^artifacts/' + NOTE_ID + '/file', 'advisornotes.views.download_artifact_file'),

    url(r'^students/' + USERID_OR_EMPLID + '/new$', 'advisornotes.views.new_note'),
    url(r'^students/' + USERID_OR_EMPLID + '/$', 'advisornotes.views.student_notes'),
    url(r'^students/' + NONSTUDENT_SLUG + '/$', 'advisornotes.views.student_notes'),
    url(r'^students/' + NONSTUDENT_SLUG + '/merge$', 'advisornotes.views.merge_nonstudent'),
    url(r'^students/' + USERID_OR_EMPLID + '/' + NOTE_ID + '/file', 'advisornotes.views.download_file'),
    url(r'^students/' + USERID_OR_EMPLID + '/moreinfo$', 'advisornotes.views.student_more_info'),
    url(r'^students/' + USERID_OR_EMPLID + '/courses$', 'advisornotes.views.student_courses'),
    url(r'^students/' + USERID_OR_EMPLID + '/visited/' + UNIT_SLUG, 'advisornotes.views.record_advisor_visit'),
    url(r'^students/' + USERID_OR_EMPLID + '/courses-data$', 'advisornotes.views.student_courses_data'),
    url(r'^students/' + USERID_OR_EMPLID + '/transfers-data$', 'advisornotes.views.student_transfers_data'),
    url(r'^students/' + USERID_OR_EMPLID + '/transfers$', 'advisornotes.views.student_transfers'),
    url(r'^students/' + USERID_OR_EMPLID + '/transfers-download$', 'advisornotes.views.student_transfers_download'),

    url(r'^new_prospective_student', 'advisornotes.views.new_nonstudent'),
    #url(r'^problems/$', 'advisornotes.views.view_problems'),
    #url(r'^problems/resolved/$', 'advisornotes.views.view_resolved_problems'),
    #url(r'^problems/(?P<prob_id>\d+)/$', 'advisornotes.views.edit_problem'),
]