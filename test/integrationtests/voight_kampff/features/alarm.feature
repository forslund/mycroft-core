Feature: mycroft-alarm

  Scenario: Set an alarm in the middle of the night
    Given an english speaking user
     When the user says "set an alarm for midnight"
     Then "mycroft-alarm" should reply with dialog from "confirm.alarm.dialog"
     And the user says "yes"
     Then "mycroft-alarm" should reply with dialog from "alarm.scheduled.dialog"
